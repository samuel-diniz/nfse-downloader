"""
Modo em lote: baixa as NFS-e de varios clientes de uma vez.

Le a lista de clientes (clientes.csv) e, para cada cliente ativo, reaproveita o download de
baixar_nfse.py (certificado, consulta ao ADN, filtros, XML e PDF). Um cliente com problema
nao interrompe os outros; no fim sai um relatorio (relatorios\\lote_AAAA-MM-DD_HHMM.csv).

Arquivos ao lado do programa:
  lote.ini      configuracoes comuns a todos os clientes (criado na primeira execucao)
  clientes.csv  lista de clientes: nome, cnpj, pasta (+ colunas opcionais)
  estado\\       controle de NSU por cliente (retomada); pode ser apagado sem risco
  relatorios\\   um CSV por execucao
  logs\\         um log por execucao

Uso:
  python baixar_lote.py --dados C:\\caminho   usa clientes.csv/lote.ini dessa pasta (ou defina NFSE_DADOS)
  python baixar_lote.py                       pergunta o periodo (ENTER = padrao do lote.ini)
  python baixar_lote.py --simular             mostra o que faria, sem baixar nem gravar nada
  python baixar_lote.py --cliente 121         so um cliente (por parte do nome, CNPJ ou numero)
  python baixar_lote.py --periodo 2026-10     o mes inteiro
  python baixar_lote.py --inicio 01/10/2026 --fim 31/10/2026
  python baixar_lote.py --sem-pergunta        nao pergunta nada (para tarefa agendada)
  python baixar_lote.py --refazer             ignora o controle de NSU e relê tudo
"""

import argparse
import configparser
import csv
import io
import json
import logging
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Optional

import baixar_nfse as nf
from baixar_nfse import CertificadoNaoEncontrado, ConfigError, ResultadoDownload

log = nf.log

NOME_INI = "lote.ini"
NOME_CSV = "clientes.csv"
DIAS_ALERTA_CERTIFICADO = 30

TEMPLATE_INI = r"""; ======================================================================
;  CONFIGURACAO DO MODO EM LOTE
; ======================================================================
;  Valem para TODOS os clientes da lista (clientes.csv). Linhas que
;  comecam com ";" sao so explicacoes.
; ======================================================================


[PERIODO]

; Periodo padrao, usado quando voce nao informa nada ao executar:
;   mes_anterior = do dia 1 ao ultimo dia do mes passado
;   fixo         = usa data_inicio e data_fim abaixo (formato DD/MM/AAAA)
;
; Ao executar com dois cliques, o programa PERGUNTA o periodo: ENTER aceita
; este padrao, ou voce digita outro (ex.: 01/09/2026 a 30/09/2026, ou 09/2026).
periodo = mes_anterior
data_inicio =
data_fim =


[SAIDA]

; Formato padrao: xml | pdf | ambos. Cada cliente pode ter o seu na coluna
; "formato" do clientes.csv.
;
; ATENCAO: se a pasta do cliente ja tem PDFs salvos com outros nomes (ex.:
; "N F No 148.pdf"), o PDF gerado ficaria ao lado, duplicado. Use "pdf" ou
; "ambos" so nos clientes em que isso e desejado.
formato = xml


[CERTIFICADO]

; (Opcional) thumbprint do SEU certificado (e-CPF/e-CNPJ do escritorio), usado
; como reserva para clientes que NAO tem certificado proprio instalado e deram
; procuracao. Em branco = o cliente precisa de certificado proprio, ou de
; thumbprint na coluna "thumbprint" do clientes.csv.
thumbprint_padrao =


[REDE]

; Se a unidade abaixo nao existir neste computador (tarefa agendada, outro
; usuario), o programa troca "Z:\" por este caminho de rede. O clientes.csv
; continua com as letras de unidade. Em branco = nao troca.
; Exemplo: caminho_rede = \\NOME-DO-SERVIDOR\Compartilhamento
unidade = Z:
caminho_rede =


[AMBIENTE]

; "producao" no uso normal; "homologacao" so para testes.
tipo = producao


[LOTE]

; Pausa entre um cliente e o proximo (segundos), e espera antes de repetir,
; uma vez, os clientes que falharam por instabilidade do ADN.
pausa_entre_clientes = 3
espera_segunda_tentativa = 60
"""


# ==========================================================================
# Estruturas
# ==========================================================================

@dataclass
class Cliente:
    linha: int
    nome: str
    cnpj: str
    pasta: str
    ativo: bool = True
    num: str = ""
    pasta_pdf: str = ""
    thumbprint: str = ""
    formato: str = ""
    obs: str = ""


@dataclass
class Padroes:
    periodo_tipo: str
    data_inicio: Optional[date]
    data_fim: Optional[date]
    formato: str
    thumbprint_padrao: str
    unidade: str
    caminho_rede: str
    homologacao: bool
    pausa: float
    espera_segunda: float


@dataclass
class LinhaRelatorio:
    cliente: Cliente
    status: str = "PENDENTE"
    obs: str = ""
    res: Optional[ResultadoDownload] = None
    validade: str = ""
    duracao: float = 0.0
    formato: str = ""


STATUS_FALHA = {"ERRO", "INCOMPLETO", "COM ERROS", "LINHA INVALIDA"}


# ==========================================================================
# lote.ini
# ==========================================================================

def ler_texto(caminho: Path) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return caminho.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise ConfigError([f"Nao foi possivel ler {caminho.name} (problema de codificacao de texto).",
                       "Abra no Bloco de Notas e salve de novo."])


def ler_ini(caminho: Path) -> Padroes:
    cfg = configparser.ConfigParser()
    cfg.read_string(ler_texto(caminho))
    erros = []

    def obter(secao, chave, padrao=""):
        try:
            return cfg.get(secao, chave).strip()
        except (configparser.NoSectionError, configparser.NoOptionError):
            return padrao

    periodo_tipo = obter("PERIODO", "periodo", "mes_anterior").lower()
    if periodo_tipo not in ("mes_anterior", "fixo"):
        erros.append(f"[PERIODO] periodo = '{periodo_tipo}' invalido. Use 'mes_anterior' ou 'fixo'.")
    d_ini = d_fim = None
    if periodo_tipo == "fixo":
        try:
            d_ini = interpretar_data(obter("PERIODO", "data_inicio"))
            d_fim = interpretar_data(obter("PERIODO", "data_fim"))
            if d_ini > d_fim:
                erros.append("[PERIODO] data_inicio nao pode ser depois de data_fim.")
        except ValueError:
            erros.append("[PERIODO] com periodo = fixo, preencha data_inicio e data_fim (DD/MM/AAAA).")

    formato = obter("SAIDA", "formato", "xml").lower()
    if formato not in ("xml", "pdf", "ambos"):
        erros.append(f"[SAIDA] formato = '{formato}' invalido. Use 'xml', 'pdf' ou 'ambos'.")

    thumb = obter("CERTIFICADO", "thumbprint_padrao").replace(" ", "").upper()
    if thumb and not re.fullmatch(r"[A-F0-9]{40}", thumb):
        erros.append("[CERTIFICADO] thumbprint_padrao parece invalido (deveria ter 40 letras/numeros).")

    ambiente = obter("AMBIENTE", "tipo", "producao").lower()
    if ambiente not in ("producao", "homologacao"):
        erros.append(f"[AMBIENTE] tipo = '{ambiente}' invalido. Use 'producao' ou 'homologacao'.")

    try:
        pausa = float(obter("LOTE", "pausa_entre_clientes", "3"))
        espera2 = float(obter("LOTE", "espera_segunda_tentativa", "60"))
    except ValueError:
        pausa = espera2 = 0
        erros.append("[LOTE] pausa_entre_clientes e espera_segunda_tentativa devem ser numeros.")

    if erros:
        raise ConfigError(erros)
    return Padroes(periodo_tipo, d_ini, d_fim, formato, thumb, obter("REDE", "unidade", "Z:"),
                   obter("REDE", "caminho_rede"), ambiente == "homologacao", pausa, espera2)


# ==========================================================================
# Periodo
# ==========================================================================

def interpretar_data(texto: str) -> date:
    t = texto.strip()
    for formato in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, formato).date()
        except ValueError:
            continue
    raise ValueError(f"data invalida: '{texto}'")


def mes_anterior(hoje: Optional[date] = None) -> tuple:
    hoje = hoje or date.today()
    ultimo = hoje.replace(day=1) - timedelta(days=1)
    return ultimo.replace(day=1), ultimo


def mes_inteiro(ano: int, mes: int) -> tuple:
    primeiro = date(ano, mes, 1)
    proximo = date(ano + (mes == 12), mes % 12 + 1, 1)
    return primeiro, proximo - timedelta(days=1)


def interpretar_periodo(texto: str) -> tuple:
    """Aceita 'DD/MM/AAAA a DD/MM/AAAA', 'AAAA-MM-DD AAAA-MM-DD', 'MM/AAAA' ou 'AAAA-MM'."""
    t = texto.strip()
    m = re.fullmatch(r"(\d{1,2})/(\d{4})", t) or re.fullmatch(r"(\d{4})-(\d{1,2})", t)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        ano, mes = (b, a) if m.re.pattern.startswith(r"(\d{1,2})") else (a, b)
        if not 1 <= mes <= 12:
            raise ValueError("mes invalido")
        return mes_inteiro(ano, mes)
    datas = re.findall(r"\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2}", t)
    if len(datas) != 2:
        raise ValueError("informe duas datas (ex.: 01/09/2026 a 30/09/2026) ou um mes (ex.: 09/2026)")
    ini, fim = interpretar_data(datas[0]), interpretar_data(datas[1])
    if ini > fim:
        raise ValueError("a data inicial e depois da final")
    return ini, fim


def fmt_data(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def resolver_periodo(args, padroes: Padroes) -> tuple:
    padrao = padroes.data_inicio, padroes.data_fim
    if padroes.periodo_tipo != "fixo":
        padrao = mes_anterior()
    try:
        if args.periodo:
            return interpretar_periodo(args.periodo)
        if args.inicio or args.fim:
            if not (args.inicio and args.fim):
                raise ValueError("use --inicio e --fim juntos")
            return interpretar_periodo(f"{args.inicio} {args.fim}")
    except ValueError as exc:
        raise ConfigError([f"Periodo informado invalido: {exc}."])
    if sys.stdin.isatty() and not args.sem_pergunta and not args.simular:
        while True:
            resposta = input(f"Periodo (ENTER = {fmt_data(padrao[0])} a {fmt_data(padrao[1])}): ").strip()
            if not resposta:
                return padrao
            try:
                return interpretar_periodo(resposta)
            except ValueError as exc:
                print(f"  Periodo invalido: {exc}.")
    return padrao


# ==========================================================================
# clientes.csv
# ==========================================================================

ALIASES = {
    "num": {"n", "no", "numero", "num", "codigo", "cod"},
    "ativo": {"ativo"},
    "nome": {"nome", "cliente", "nome cliente", "nome do cliente"},
    "cnpj": {"cnpj"},
    "pasta": {"pasta", "caminho", "caminho da pasta", "pasta destino"},
    "pasta_pdf": {"pasta pdf", "pasta_pdf", "pasta dos pdfs"},
    "thumbprint": {"thumbprint", "impressao digital"},
    "formato": {"formato"},
    "obs": {"obs", "observacao", "observacoes"},
}


def normalizar_cabecalho(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def normalizar_cnpj(texto: str) -> tuple:
    """Retorna (cnpj, aviso). Recompoe zeros que o Excel tenha comido; recusa o que nao fecha 14 digitos."""
    d = re.sub(r"\D", "", texto or "")
    if re.search(r"[eE]\+\d+", texto or ""):
        return "", "CNPJ em notacao cientifica: o Excel alterou o numero (formate a coluna como Texto)"
    if 11 <= len(d) < 14:
        return d.zfill(14), "CNPJ com menos de 14 digitos: zeros a esquerda recompostos"
    if len(d) != 14:
        return "", f"CNPJ invalido ('{texto}'): precisa ter 14 digitos"
    return d, ""


def ler_clientes(caminho: Path) -> tuple:
    """Retorna (clientes, problemas); problemas = [(linha, nome, mensagem)]."""
    texto = ler_texto(caminho)
    primeira = texto.splitlines()[0] if texto.strip() else ""
    delimitador = ";" if primeira.count(";") >= primeira.count(",") else ","
    leitor = csv.reader(io.StringIO(texto), delimiter=delimitador)
    linhas = list(leitor)
    if not linhas:
        raise ConfigError([f"{caminho.name} esta vazio."])

    mapa = {}
    for i, cab in enumerate(linhas[0]):
        nome_cab = normalizar_cabecalho(cab)
        for campo, nomes in ALIASES.items():
            if nome_cab in nomes and campo not in mapa:
                mapa[campo] = i
    faltando = [c for c in ("nome", "cnpj", "pasta") if c not in mapa]
    if faltando:
        raise ConfigError([f"{caminho.name}: faltam as colunas {', '.join(faltando)}.",
                           f"Colunas encontradas: {', '.join(c.strip() for c in linhas[0])}."])

    clientes, problemas, vistos = [], [], {}
    for n, linha in enumerate(linhas[1:], start=2):
        if not any(c.strip() for c in linha):
            continue

        def campo(nome):
            i = mapa.get(nome)
            return linha[i].strip() if i is not None and i < len(linha) else ""

        nome = campo("nome") or f"(linha {n})"
        erros = []
        cnpj, aviso = normalizar_cnpj(campo("cnpj"))
        if not cnpj:
            erros.append(aviso)
        pasta = campo("pasta")
        if not pasta:
            erros.append("pasta em branco")
        formato = campo("formato").lower()
        if formato and formato not in ("xml", "pdf", "ambos"):
            erros.append(f"formato '{formato}' invalido (use xml, pdf ou ambos)")
        thumb = campo("thumbprint").replace(" ", "").upper()
        if thumb and not re.fullmatch(r"[A-F0-9]{40}", thumb):
            erros.append("thumbprint invalido (deveria ter 40 letras/numeros)")
        ativo_txt = campo("ativo").lower()
        ativo = ativo_txt not in ("nao", "n", "0", "false", "inativo")
        if cnpj and cnpj in vistos and ativo:
            erros.append(f"CNPJ repetido (ja aparece na linha {vistos[cnpj]})")
        if erros:
            problemas.append((n, nome, "; ".join(erros)))
            continue
        if ativo:
            vistos[cnpj] = n
        obs = campo("obs")
        clientes.append(Cliente(n, nome, cnpj, pasta, ativo, campo("num"), campo("pasta_pdf"), thumb, formato,
                                (obs + " | " if obs and aviso else obs) + aviso))
    return clientes, problemas


def filtrar_clientes(clientes: list, termos: list) -> list:
    if not termos:
        return clientes
    escolhidos = []
    for c in clientes:
        nome_n = normalizar_cabecalho(c.nome)
        for t in termos:
            tn = normalizar_cabecalho(t)
            digitos = re.sub(r"\D", "", t)
            if (tn and tn in nome_n) or (digitos and digitos == c.cnpj) or (t.strip() and t.strip() == c.num):
                escolhidos.append(c)
                break
    return escolhidos


# ==========================================================================
# Pastas (Z: -> caminho de rede)
# ==========================================================================

def traduzir_unidade(caminho: str, unidade: str, caminho_rede: str, existe=lambda p: Path(p).exists()) -> str:
    """Troca 'Z:\\' pelo caminho de rede quando a letra nao existe neste computador."""
    c = caminho.strip()
    prefixo = unidade.rstrip("\\")
    if prefixo and caminho_rede and c.upper().startswith(prefixo.upper() + "\\") and not existe(prefixo + "\\"):
        return caminho_rede.rstrip("\\") + c[len(prefixo):]
    return c


# ==========================================================================
# Estado (retomada por NSU)
# ==========================================================================

def caminho_estado(cnpj: str) -> Path:
    return nf.pasta_base() / "estado" / f"{cnpj}.json"


def carregar_estado(cnpj: str) -> list:
    try:
        return json.loads(caminho_estado(cnpj).read_text(encoding="utf-8")).get("cobertura", [])
    except (OSError, ValueError):
        return []


def salvar_estado(cnpj: str, cobertura: list) -> None:
    destino = caminho_estado(cnpj)
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporario = destino.with_suffix(".tmp")
    temporario.write_text(json.dumps({"cobertura": cobertura}, ensure_ascii=False, indent=1), encoding="utf-8")
    temporario.replace(destino)


def nsu_para_retomar(cobertura: list, ini: date, fim: date, xml_dir: Optional[str], pdf_dir: Optional[str]) -> int:
    """
    So retoma de um NSU se alguma execucao anterior ja cobriu TODO o periodo pedido, com o mesmo
    formato e as mesmas pastas: aquela execucao viu e gravou todas as notas do periodo ate esse NSU.
    Qualquer outro caso relê desde o inicio (as notas fora do periodo anterior foram ignoradas, e
    um "ultimo NSU" simples as perderia).
    """
    validos = [e["nsu"] for e in cobertura
               if e["inicio"] <= ini.isoformat() and e["fim"] >= fim.isoformat()
               and (xml_dir is None or e.get("xml") == xml_dir)
               and (pdf_dir is None or e.get("pdf") == pdf_dir)]
    return max(validos, default=0)


def registrar_cobertura(cobertura: list, ini: date, fim: date, nsu: int,
                        xml_dir: Optional[str], pdf_dir: Optional[str]) -> list:
    nova = {"inicio": ini.isoformat(), "fim": fim.isoformat(), "nsu": nsu, "xml": xml_dir, "pdf": pdf_dir,
            "em": datetime.now().isoformat(timespec="seconds")}

    def dominada(e):
        return (nova["inicio"] <= e["inicio"] and e["fim"] <= nova["fim"] and e["nsu"] <= nsu
                and (e.get("xml") is None or e.get("xml") == xml_dir)
                and (e.get("pdf") is None or e.get("pdf") == pdf_dir))

    return ([e for e in cobertura if not dominada(e)] + [nova])[-40:]


# ==========================================================================
# Certificado
# ==========================================================================

@lru_cache(maxsize=None)
def _certificado_manual(thumbprint: str) -> tuple:
    return nf.validar_thumbprint_manual(thumbprint)


def resolver_certificado(cliente: Cliente, padroes: Padroes) -> tuple:
    if cliente.thumbprint:
        return _certificado_manual(cliente.thumbprint)
    try:
        return nf.localizar_certificado(cliente.cnpj, "")
    except CertificadoNaoEncontrado:
        if padroes.thumbprint_padrao:
            thumbprint, descricao = _certificado_manual(padroes.thumbprint_padrao)
            return thumbprint, descricao + " [certificado padrao: acesso por procuracao]"
        raise


def validade_do_certificado(descricao: str) -> str:
    m = re.search(r"valido ate (\d{4}-\d{2}-\d{2})", descricao)
    return m.group(1) if m else ""


# ==========================================================================
# Processamento
# ==========================================================================

def processar_cliente(cliente: Cliente, ini: date, fim: date, padroes: Padroes, refazer: bool) -> LinhaRelatorio:
    inicio = time.perf_counter()
    linha = LinhaRelatorio(cliente, formato=cliente.formato or padroes.formato)
    try:
        thumbprint, descricao = resolver_certificado(cliente, padroes)
        linha.validade = validade_do_certificado(descricao)

        pasta = Path(traduzir_unidade(cliente.pasta, padroes.unidade, padroes.caminho_rede))
        pasta_pdf = Path(traduzir_unidade(cliente.pasta_pdf, padroes.unidade, padroes.caminho_rede)) \
            if cliente.pasta_pdf else pasta
        config = nf.Configuracao(
            nome_cliente=cliente.nome, cnpj=cliente.cnpj, thumbprint_manual=cliente.thumbprint,
            data_inicio=datetime.combine(ini, datetime.min.time()), data_fim=datetime.combine(fim, datetime.min.time()),
            pasta_destino=pasta, pasta_pdf=pasta_pdf, formato=linha.formato, usar_producao_restrita=padroes.homologacao)

        quer_xml, quer_pdf = linha.formato in ("xml", "ambos"), linha.formato in ("pdf", "ambos")
        xml_dir, pdf_dir = (str(pasta) if quer_xml else None), (str(pasta_pdf) if quer_pdf else None)
        cobertura = carregar_estado(cliente.cnpj)
        nsu_ini = 0 if refazer else nsu_para_retomar(cobertura, ini, fim, xml_dir, pdf_dir)

        res = nf.executar_download(config, thumbprint, descricao, nsu_inicial=nsu_ini, criar_pasta_base=False)
        linha.res = res

        if not res.completo:
            linha.status, linha.obs = "INCOMPLETO", res.erro or "download interrompido"
        elif res.erros:
            linha.status, linha.obs = "COM ERROS", f"{res.erros} erro(s) ao processar/gravar notas (veja o log)"
        elif res.nsu_inicial == 0 and res.docs_vistos == 0:
            linha.status = "SEM NOTAS"
            linha.obs = "o ADN nao retornou nenhum documento: conferir CNPJ, certificado e procuracao"
        else:
            linha.status = "OK"
            sem_notas = not (res.xml_novos or res.xml_pulados or res.pdf_novos or res.pdf_pulados)
            if res.nsu_inicial:
                linha.obs = f"retomou do NSU {res.nsu_inicial}" + ("; sem notas novas desde a ultima execucao" if sem_notas else "")
            elif sem_notas:
                linha.obs = "nenhuma nota emitida no periodo"
        if res.completo and not res.erros:
            salvar_estado(cliente.cnpj, registrar_cobertura(cobertura, ini, fim, res.ultimo_nsu, xml_dir, pdf_dir))
    except ConfigError as exc:
        linha.status, linha.obs = "ERRO", exc.mensagens[0]
        for m in exc.mensagens:
            log.error("%s", m)
    except Exception as exc:  # um cliente nunca derruba os demais
        linha.status, linha.obs = "ERRO", f"erro inesperado: {exc}"
        log.exception("Erro inesperado no cliente %s", cliente.nome)
    linha.duracao = time.perf_counter() - inicio
    return linha


def validar_antes(ativos: list, padroes: Padroes) -> list:
    """Retorna [(cliente, pasta_traduzida, existe)] para as pastas XML/PDF de cada cliente ativo."""
    saida = []
    for c in ativos:
        pasta = traduzir_unidade(c.pasta, padroes.unidade, padroes.caminho_rede)
        saida.append((c, pasta, Path(pasta).exists()))
    return saida


# ==========================================================================
# Saidas
# ==========================================================================

def descricao_resumida(l: LinhaRelatorio) -> tuple:
    r = l.res
    xml = str(r.xml_novos) if r and l.formato in ("xml", "ambos") else "-"
    pdf = str(r.pdf_novos) if r and l.formato in ("pdf", "ambos") else "-"
    return xml, pdf


def imprimir_tabela(linhas: list) -> None:
    cab = ("Cliente", "Status", "XML", "PDF", "Observacao")
    dados = [(l.cliente.nome[:34], l.status, *descricao_resumida(l), l.obs[:70]) for l in linhas]
    larguras = [max(len(str(x[i])) for x in [cab] + dados) for i in range(5)]
    fmt = "  ".join("{:<%d}" % w for w in larguras)
    print(fmt.format(*cab))
    print(fmt.format(*("-" * w for w in larguras)))
    for d in dados:
        print(fmt.format(*d))


def gravar_relatorio(linhas: list, ini: date, fim: date, destino: Path) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    cab = ["cliente", "cnpj", "status", "periodo", "formato", "xml_novos", "xml_pulados", "pdf_novos", "pdf_pulados",
           "fora_do_periodo", "recebidas", "erros", "nsu_inicial", "ultimo_nsu", "validade_certificado",
           "duracao_s", "observacao"]
    with open(destino, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(cab)
        for l in linhas:
            r = l.res or ResultadoDownload()
            w.writerow([l.cliente.nome, l.cliente.cnpj, l.status, f"{fmt_data(ini)} a {fmt_data(fim)}", l.formato,
                        r.xml_novos, r.xml_pulados, r.pdf_novos, r.pdf_pulados, r.fora_periodo, r.recebidas,
                        r.erros, r.nsu_inicial, r.ultimo_nsu, l.validade, round(l.duracao, 1), l.obs])


def avisos_de_certificado(linhas: list) -> list:
    avisos = []
    for l in linhas:
        if l.validade:
            dias = (datetime.strptime(l.validade, "%Y-%m-%d").date() - date.today()).days
            if dias <= DIAS_ALERTA_CERTIFICADO:
                avisos.append(f"{l.cliente.nome}: certificado vence em {l.validade} ({dias} dia(s))")
    return avisos


def simular(ativos: list, ini: date, fim: date, padroes: Padroes, refazer: bool) -> None:
    print(f"SIMULACAO (nada sera baixado nem gravado). Periodo: {fmt_data(ini)} a {fmt_data(fim)}")
    for c in ativos:
        formato = c.formato or padroes.formato
        pasta = traduzir_unidade(c.pasta, padroes.unidade, padroes.caminho_rede)
        try:
            thumbprint, descricao = resolver_certificado(c, padroes)
            cert = "ok" + (" (padrao/procuracao)" if "certificado padrao" in descricao else "") \
                   + (f", vence {validade_do_certificado(descricao)}" if validade_do_certificado(descricao) else "")
        except ConfigError as exc:
            cert = "PROBLEMA: " + exc.mensagens[0]
        quer_xml, quer_pdf = formato in ("xml", "ambos"), formato in ("pdf", "ambos")
        pasta_pdf = traduzir_unidade(c.pasta_pdf, padroes.unidade, padroes.caminho_rede) if c.pasta_pdf else pasta
        nsu = 0 if refazer else nsu_para_retomar(carregar_estado(c.cnpj), ini, fim,
                                                 pasta if quer_xml else None, pasta_pdf if quer_pdf else None)
        print(f"- {c.nome} | formato={formato} | pasta {'ok' if Path(pasta).exists() else 'NAO ENCONTRADA'} | "
              f"certificado: {cert} | comeca do NSU {nsu}")


# ==========================================================================
# Ponto de entrada
# ==========================================================================

def criar_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Baixa as NFS-e de varios clientes (modo em lote).")
    p.add_argument("--dados", help=f"pasta com {NOME_CSV} e {NOME_INI}; estado, logs e relatorios tambem ficam nela "
                                   "(padrao: ao lado do programa, ou a variavel de ambiente NFSE_DADOS)")
    p.add_argument("--versao", action="version", version=f"nfse-downloader {nf.VERSAO}")
    p.add_argument("--clientes", help=f"caminho do CSV de clientes (padrao: {NOME_CSV} ao lado do programa)")
    p.add_argument("--ini", help=f"caminho do .ini do lote (padrao: {NOME_INI} ao lado do programa)")
    p.add_argument("--cliente", action="append", default=[], help="so este cliente (nome, CNPJ ou numero); pode repetir")
    p.add_argument("--periodo", help="mes inteiro: AAAA-MM ou MM/AAAA")
    p.add_argument("--inicio", help="data inicial (DD/MM/AAAA)")
    p.add_argument("--fim", help="data final (DD/MM/AAAA)")
    p.add_argument("--simular", action="store_true", help="mostra o que faria, sem baixar nem gravar")
    p.add_argument("--refazer", action="store_true", help="ignora o controle de NSU e relê o historico todo")
    p.add_argument("--sem-pergunta", action="store_true", help="nao pergunta o periodo (tarefa agendada)")
    return p


def principal(argv=None) -> int:
    args = criar_parser().parse_args(argv)
    nf.definir_pasta_dados(args.dados)
    base = nf.validar_pasta_dados()
    print(f"NFS-e downloader v{nf.VERSAO} - modo em lote")
    caminho_ini = Path(args.ini) if args.ini else base / NOME_INI
    caminho_csv = Path(args.clientes) if args.clientes else base / NOME_CSV

    if not caminho_ini.exists():
        caminho_ini.write_text(TEMPLATE_INI, encoding="utf-8")
        print("=" * 72)
        print(f"Arquivo de configuracao do lote criado: {caminho_ini}")
        print("Confira os padroes e crie/ajuste o clientes.csv, depois rode de novo.")
        print("=" * 72)
        return 0
    if not caminho_csv.exists():
        raise ConfigError([f"Lista de clientes nao encontrada: {caminho_csv}",
                           "Crie o clientes.csv com as colunas: ativo;nome;cnpj;pasta (as demais sao opcionais)."])

    padroes = ler_ini(caminho_ini)
    clientes, problemas = ler_clientes(caminho_csv)
    selecionados = filtrar_clientes(clientes, args.cliente)
    ativos = [c for c in selecionados if c.ativo]
    if args.cliente and not selecionados:
        raise ConfigError([f"Nenhum cliente da lista corresponde a: {', '.join(args.cliente)}"])
    if not ativos:
        raise ConfigError(["Nenhum cliente ativo para processar."])

    ini, fim = resolver_periodo(args, padroes)
    print(f"Periodo: {fmt_data(ini)} a {fmt_data(fim)} | clientes ativos: {len(ativos)}"
          + (f" | linhas invalidas no CSV: {len(problemas)}" if problemas else ""))
    for n, nome, msg in problemas:
        print(f"  [LINHA {n} IGNORADA] {nome}: {msg}")

    if args.simular:
        simular(ativos, ini, fim, padroes, args.refazer)
        return 0

    verificacao = validar_antes(ativos, padroes)
    if not any(ok for _, _, ok in verificacao):
        raise ConfigError(["Nenhuma das pastas de destino esta acessivel.",
                           "Confira se o computador do servidor esta ligado, a rede conectada e a unidade "
                           f"{padroes.unidade} mapeada (ou o caminho de rede em {NOME_INI})."])

    agora = datetime.now().strftime("%Y-%m-%d_%H%M")
    pasta_logs = base / "logs"
    pasta_logs.mkdir(exist_ok=True)
    arquivo_log = logging.FileHandler(pasta_logs / f"lote_{agora}.log", encoding="utf-8")
    arquivo_log.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    logging.getLogger().addHandler(arquivo_log)

    linhas = []
    for n, (n_, nome, msg) in enumerate(problemas):
        linhas.append(LinhaRelatorio(Cliente(n_, nome, "", ""), "LINHA INVALIDA", msg))
    resultados = {}
    for i, cliente in enumerate(ativos, start=1):
        log.info("==================== [%d/%d] %s ====================", i, len(ativos), cliente.nome)
        resultados[cliente.cnpj] = processar_cliente(cliente, ini, fim, padroes, args.refazer)
        if i < len(ativos):
            time.sleep(padroes.pausa)

    instaveis = [c for c in ativos if resultados[c.cnpj].status == "INCOMPLETO"]
    if instaveis:
        log.warning("%d cliente(s) falharam por instabilidade; nova tentativa em %.0f s.", len(instaveis), padroes.espera_segunda)
        time.sleep(padroes.espera_segunda)
        for cliente in instaveis:
            log.info("==================== SEGUNDA TENTATIVA: %s ====================", cliente.nome)
            resultados[cliente.cnpj] = processar_cliente(cliente, ini, fim, padroes, args.refazer)
            time.sleep(padroes.pausa)

    linhas += [resultados[c.cnpj] for c in ativos]
    print()
    print("=" * 30 + " RESULTADO DO LOTE " + "=" * 30)
    imprimir_tabela(linhas)
    avisos = avisos_de_certificado(linhas)
    if avisos:
        print("\nCertificados que vencem em breve:")
        for a in avisos:
            print(f"  - {a}")
    destino_relatorio = base / "relatorios" / f"lote_{agora}.csv"
    gravar_relatorio(linhas, ini, fim, destino_relatorio)
    print(f"\nRelatorio: {destino_relatorio}")
    print(f"Log:       {pasta_logs / f'lote_{agora}.log'}")
    falhas = [l for l in linhas if l.status in STATUS_FALHA]
    print(f"\nClientes com falha: {len(falhas)} de {len(linhas)}" if falhas else "\nTodos os clientes concluidos.")
    return 1 if falhas else 0


def main() -> None:
    codigo = 0
    esperar = True
    try:
        codigo = principal()
    except SystemExit:  # --versao, --help ou erro de argumento: nao precisa esperar ENTER
        esperar = False
        raise
    except ConfigError as exc:
        print("=" * 72)
        print("Nao foi possivel continuar por causa do(s) problema(s) abaixo:")
        print("=" * 72)
        for msg in exc.mensagens:
            print(f"- {msg}")
        print("=" * 72)
        codigo = 2
    except KeyboardInterrupt:
        print("\nInterrompido pelo usuario. O que ja foi gravado esta salvo; rode de novo para continuar.")
        codigo = 130
    except Exception:
        import traceback
        print("=" * 72)
        print("Ocorreu um erro inesperado:")
        print("=" * 72)
        traceback.print_exc()
        print("=" * 72)
        print("Copie esta mensagem e envie para quem cuida do sistema.")
        codigo = 3
    finally:
        if esperar:
            nf.aguardar_saida()
    sys.exit(codigo)


if __name__ == "__main__":
    main()
