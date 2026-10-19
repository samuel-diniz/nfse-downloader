"""
Download em lote de NFS-e (NFS-e Nacional / ADN) via API oficial, com
autenticacao mTLS por certificado digital A1 ja instalado no repositorio
de certificados do Windows (Cert Store).

Este arquivo e o "motor" do programa. Toda a configuracao (cliente,
CNPJ, periodo, pasta de destino) fica fora daqui, no arquivo
"config.ini" gerado automaticamente na primeira execucao -- ver
COMO_USAR.txt para instrucoes voltadas ao usuario final.

------------------------------------------------------------------------
SOBRE O ENDPOINT E O SCHEMA DA API (referencia tecnica)
------------------------------------------------------------------------
Endpoint: GET {base}/DFe/{NSU}?cnpjConsulta={cnpj}&lote=true
  (o CNPJ NAO entra no path da URL -- ele e validado contra o CNPJ do
  certificado usado na conexao; "cnpjConsulta" so e necessario quando se
  quer consultar um CNPJ diferente do CNPJ raiz do certificado).

Resposta (schema LoteDistribuicaoNSUResponse, confirmado via swagger.json
do proprio ADN):
  - StatusProcessamento: "DOCUMENTOS_LOCALIZADOS" | "NENHUM_DOCUMENTO_LOCALIZADO" | "REJEICAO"
  - LoteDFe: lista de documentos, cada um com NSU, ChaveAcesso, ArquivoXml
    (XML compactado em GZip + Base64), DataHoraGeracao, TipoDocumento
  - Erros / Alertas: listas de mensagens de processamento

A API responde HTTP 404 tanto para "fim da paginacao" (StatusProcessamento
NENHUM_DOCUMENTO_LOCALIZADO) quanto para outros erros -- por isso o corpo
da resposta e sempre inspecionado, nao so o codigo HTTP.

O lote de distribuicao tambem traz eventos (cancelamento, confirmacao...)
e notas onde o contribuinte figura como TOMADOR (servico recebido de
terceiros, nao emitido por ele). O programa filtra para so salvar
documentos TipoDocumento=="NFSE" cujo <prest><CNPJ> bate com o CNPJ
configurado -- ou seja, so notas realmente emitidas pelo cliente.
"""

import base64
import configparser
import gzip
import io
import json
import logging
import re
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# ==========================================================================
# Constantes tecnicas (nao mudam de cliente para cliente -- nao ficam no
# config.ini para nao poluir o que o usuario final precisa preencher).
# ==========================================================================

CURL_EXE = r"C:\Windows\System32\curl.exe"
POWERSHELL_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

URL_BASE_PRODUCAO = "https://adn.nfse.gov.br/contribuintes"
URL_BASE_PRODUCAO_RESTRITA = "https://adn.producaorestrita.nfse.gov.br/contribuintes"

NSU_INICIAL = 0
MAX_TENTATIVAS = 3
BACKOFF_SEGUNDOS = 2

NOME_ARQUIVO_CONFIG = "config.ini"
MARCADOR_STATUS = "\n__HTTP_STATUS__:"

TEMPLATE_CONFIG = """\
; ======================================================================
;  CONFIGURACAO - Download de NFS-e (ADN / NFS-e Nacional)
; ======================================================================
;
;  Como usar:
;    1. Preencha os campos abaixo (depois do sinal de "=").
;    2. Salve este arquivo (Ctrl+S) e feche o Bloco de Notas.
;    3. De dois cliques em "baixar_nfse.exe".
;
;  Linhas que comecam com ";" sao apenas explicacoes e sao ignoradas
;  pelo programa. Um arquivo config.ini serve para UM cliente. Para
;  baixar de outro cliente, copie a pasta inteira e edite a copia.
; ======================================================================


[CLIENTE]

; Nome do cliente. Serve so para identificacao nas mensagens do
; programa -- nao influencia o download.
nome =

; CNPJ do cliente, somente numeros (sem ponto, barra ou traco).
; Exemplo: 12345678000199
cnpj =


[CERTIFICADO]

; NA MAIORIA DOS CASOS, NAO PRECISA PREENCHER NADA AQUI.
;
; O programa procura sozinho, no repositorio de certificados do Windows
; deste computador, o certificado digital A1 cujo CNPJ bate com o
; informado acima em [CLIENTE] -> cnpj. E o mesmo certificado usado
; para acessar o e-CAC, por exemplo -- ele precisa estar instalado
; neste computador, no usuario do Windows que vai rodar o programa.
;
; So preencha "thumbprint" abaixo se o programa avisar que:
;   a) encontrou mais de um certificado valido para o mesmo CNPJ, ou
;   b) voce quer forcar um certificado especifico.
; Nesses casos, copie o "thumbprint" que o proprio programa mostrar na
; mensagem e cole aqui.
thumbprint =


[PERIODO]

; Periodo de busca das notas, pela DATA DE EMISSAO.
; Formato obrigatorio: AAAA-MM-DD (ano-mes-dia)
;
; Exemplo para baixar o mes de agosto de 2026 inteiro:
;   data_inicio = 2026-08-01
;   data_fim    = 2026-08-31
data_inicio =
data_fim =


[DESTINO]

; Pasta onde os XMLs serao salvos. Use o caminho completo, como aparece
; na barra de enderecos do Windows Explorer. O programa cria sozinho,
; dentro dessa pasta, uma subpasta para cada mes (01, 02, 03...).
;
; Exemplo:
;   pasta = Z:\\FISCAL\\2026\\NFSE\\NOME DO CLIENTE\\Prestacao
pasta =


[AMBIENTE]

; Deixe "producao" para o uso normal do dia a dia (notas fiscais reais).
; So mude para "homologacao" se for pedido explicitamente para testar
; antes de usar em producao.
tipo = producao
"""


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nfse")


class ConfigError(Exception):
    """Erro de configuracao com mensagens ja prontas para mostrar ao usuario final."""

    def __init__(self, mensagens):
        self.mensagens = mensagens if isinstance(mensagens, list) else [mensagens]
        super().__init__("\n".join(self.mensagens))


@dataclass
class Configuracao:
    nome_cliente: str
    cnpj: str
    thumbprint_manual: str
    data_inicio: datetime
    data_fim: datetime
    pasta_destino: Path
    usar_producao_restrita: bool


# ==========================================================================
# Localizacao do config.ini (ao lado do .exe, ou ao lado do .py em dev)
# ==========================================================================

def pasta_base() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def caminho_config() -> Path:
    return pasta_base() / NOME_ARQUIVO_CONFIG


def criar_config_padrao() -> None:
    caminho_config().write_text(TEMPLATE_CONFIG, encoding="utf-8")


def ler_texto_config() -> str:
    caminho = caminho_config()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return caminho.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise ConfigError([
        f"Nao foi possivel ler o arquivo {caminho.name} (problema de codificacao de texto).",
        "Abra o arquivo no Bloco de Notas, salve novamente e tente de novo.",
    ])


def carregar_configuracao() -> Configuracao:
    cfg = configparser.ConfigParser()
    cfg.read_string(ler_texto_config())

    erros = []

    def obter(secao: str, chave: str, obrigatorio: bool = True, padrao: str = "") -> str:
        try:
            valor = cfg.get(secao, chave).strip()
        except (configparser.NoSectionError, configparser.NoOptionError):
            valor = padrao
        if obrigatorio and not valor:
            erros.append(f"[{secao}] {chave} nao pode ficar em branco.")
        return valor

    nome_cliente = obter("CLIENTE", "nome", obrigatorio=False)

    cnpj_bruto = obter("CLIENTE", "cnpj")
    cnpj = re.sub(r"\D", "", cnpj_bruto)
    if cnpj_bruto and len(cnpj) != 14:
        erros.append(f"[CLIENTE] cnpj = '{cnpj_bruto}' invalido. Deve ter 14 digitos, somente numeros.")

    thumbprint_manual = obter("CERTIFICADO", "thumbprint", obrigatorio=False).replace(" ", "").upper()
    if thumbprint_manual and not re.fullmatch(r"[A-F0-9]{40}", thumbprint_manual):
        erros.append("[CERTIFICADO] thumbprint informado parece invalido (deveria ter 40 letras/numeros).")

    data_inicio_txt = obter("PERIODO", "data_inicio")
    data_fim_txt = obter("PERIODO", "data_fim")
    data_inicio = data_fim = None
    for rotulo, txt in (("data_inicio", data_inicio_txt), ("data_fim", data_fim_txt)):
        if not txt:
            continue
        try:
            valor = datetime.strptime(txt, "%Y-%m-%d")
        except ValueError:
            erros.append(f"[PERIODO] {rotulo} = '{txt}' invalido. Use o formato AAAA-MM-DD, ex: 2026-01-31.")
            continue
        if rotulo == "data_inicio":
            data_inicio = valor
        else:
            data_fim = valor

    if data_inicio and data_fim and data_inicio > data_fim:
        erros.append("[PERIODO] data_inicio nao pode ser depois de data_fim.")

    pasta_txt = obter("DESTINO", "pasta")
    pasta_destino = Path(pasta_txt) if pasta_txt else None

    ambiente_txt = obter("AMBIENTE", "tipo", obrigatorio=False, padrao="producao").strip().lower()
    if ambiente_txt not in ("producao", "homologacao"):
        erros.append(f"[AMBIENTE] tipo = '{ambiente_txt}' invalido. Use 'producao' ou 'homologacao'.")
    usar_producao_restrita = ambiente_txt == "homologacao"

    if erros:
        raise ConfigError(erros)

    return Configuracao(
        nome_cliente=nome_cliente or cnpj,
        cnpj=cnpj,
        thumbprint_manual=thumbprint_manual,
        data_inicio=data_inicio,
        data_fim=data_fim,
        pasta_destino=pasta_destino,
        usar_producao_restrita=usar_producao_restrita,
    )


# ==========================================================================
# Localizacao automatica do certificado no Windows Cert Store, por CNPJ
# ==========================================================================

def localizar_certificado(cnpj: str, thumbprint_manual: str) -> tuple:
    """Retorna (thumbprint, descricao) do certificado a ser usado nas chamadas."""
    if thumbprint_manual:
        return thumbprint_manual, "informado manualmente no config.ini"

    comando_ps = (
        f"Get-ChildItem Cert:\\CurrentUser\\My | "
        f"Where-Object {{ $_.Subject -match '{cnpj}' -and $_.HasPrivateKey }} | "
        f"ForEach-Object {{ \"$($_.Thumbprint)|$($_.NotAfter.ToString('yyyy-MM-dd'))|$($_.Subject)\" }}"
    )
    resultado = subprocess.run(
        [POWERSHELL_EXE, "-NoProfile", "-Command", comando_ps],
        capture_output=True, text=True, timeout=30,
    )
    linhas = [l.strip() for l in resultado.stdout.splitlines() if l.strip()]

    candidatos = []
    for linha in linhas:
        partes = linha.split("|", 2)
        if len(partes) == 3:
            candidatos.append(tuple(partes))

    if not candidatos:
        raise ConfigError([
            f"Nenhum certificado digital com o CNPJ {cnpj} foi encontrado neste computador.",
            "Verifique se o certificado A1 do cliente esta instalado no Windows deste usuario",
            "(o mesmo usado para acessar o e-CAC, por exemplo).",
        ])

    hoje = datetime.now()
    validos = []
    vencidos = []
    for thumbprint, validade_txt, subject in candidatos:
        try:
            validade = datetime.strptime(validade_txt, "%Y-%m-%d")
        except ValueError:
            validade = None
        if validade and validade < hoje:
            vencidos.append((thumbprint, validade_txt, subject))
        else:
            validos.append((thumbprint, validade_txt, subject))

    if not validos:
        thumbprint, validade_txt, subject = vencidos[0]
        raise ConfigError([
            f"Foi encontrado um certificado para o CNPJ {cnpj}, mas esta vencido desde {validade_txt}.",
            f"Certificado: {subject}",
            "Providencie a renovacao do certificado digital com o cliente.",
        ])

    if len(validos) > 1:
        linhas_erro = [f"Foram encontrados {len(validos)} certificados validos para o CNPJ {cnpj}:"]
        for thumbprint, validade_txt, subject in validos:
            linhas_erro.append(f"  - thumbprint={thumbprint}  validade={validade_txt}  {subject}")
        linhas_erro.append("Abra o config.ini e preencha [CERTIFICADO] thumbprint com o correto.")
        raise ConfigError(linhas_erro)

    thumbprint, validade_txt, subject = validos[0]
    return thumbprint, f"{subject} (valido ate {validade_txt})"


# ==========================================================================
# Chamadas a API (mTLS via curl.exe + certificado do Windows Cert Store)
# ==========================================================================

def chamar_api(url: str, thumbprint: str) -> tuple:
    """
    Faz GET via curl.exe usando o certificado do Windows Cert Store
    (mTLS/Schannel, sem senha, sem exportar a chave privada).
    Retorna (status_http, corpo_texto).
    """
    cert_ref = f"CurrentUser\\MY\\{thumbprint}"
    comando = [
        CURL_EXE,
        "-s",
        "--cert", cert_ref,
        "-H", "Accept: application/json",
        "-w", MARCADOR_STATUS + "%{http_code}",
        url,
    ]
    resultado = subprocess.run(comando, capture_output=True, text=True, encoding="utf-8", timeout=60)

    if resultado.returncode != 0:
        raise RuntimeError(f"curl.exe falhou (codigo {resultado.returncode}): {resultado.stderr.strip()}")

    saida = resultado.stdout
    corpo, _, status_texto = saida.rpartition(MARCADOR_STATUS)
    if not status_texto:
        raise RuntimeError(f"Resposta inesperada do curl.exe (sem status HTTP): {saida[:300]}")

    return int(status_texto.strip()), corpo


def chamar_com_retry(url: str, thumbprint: str) -> tuple:
    """
    Chama a API com algumas tentativas em caso de falha de rede/servidor
    ou de limite de requisicoes (HTTP 429), que a API do ADN aplica de
    forma bastante agressiva mesmo com poucas chamadas seguidas.
    """
    ultima_excecao = None
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            status, corpo = chamar_api(url, thumbprint)
            if status == 429:
                espera = BACKOFF_SEGUNDOS * (2 ** tentativa)
                log.warning(
                    "Tentativa %d/%d: HTTP 429 (limite de requisicoes) -- aguardando %ds",
                    tentativa, MAX_TENTATIVAS, espera,
                )
                if tentativa < MAX_TENTATIVAS:
                    time.sleep(espera)
                continue
            if status >= 500:
                log.warning("Tentativa %d/%d: HTTP %d ao chamar %s", tentativa, MAX_TENTATIVAS, status, url)
            else:
                return status, corpo
        except (RuntimeError, subprocess.SubprocessError) as exc:
            ultima_excecao = exc
            log.warning("Tentativa %d/%d: erro ao chamar %s -- %s", tentativa, MAX_TENTATIVAS, url, exc)

        if tentativa < MAX_TENTATIVAS:
            time.sleep(BACKOFF_SEGUNDOS * tentativa)

    if ultima_excecao:
        raise ultima_excecao
    raise RuntimeError(f"Falha ao chamar {url} apos {MAX_TENTATIVAS} tentativas (HTTP 429/5xx persistente)")


# ==========================================================================
# Processamento do XML
# ==========================================================================

def descompactar_xml(conteudo_b64: str) -> str:
    """Decodifica Base64 e descompacta GZip, retornando o XML como texto."""
    dados_gzip = base64.b64decode(conteudo_b64)
    with gzip.GzipFile(fileobj=io.BytesIO(dados_gzip)) as f:
        return f.read().decode("utf-8")


def extrair_data_emissao(xml_texto: str) -> datetime:
    """Le a tag <dhEmi> do XML da NFS-e e retorna a data de emissao."""
    raiz = ET.fromstring(xml_texto)
    for elem in raiz.iter():
        if elem.tag.split("}")[-1] == "dhEmi" and elem.text:
            texto_data = elem.text.strip()[:10]
            return datetime.strptime(texto_data, "%Y-%m-%d")
    raise ValueError("Tag <dhEmi> nao encontrada no XML")


def extrair_cnpj_prestador(xml_texto: str) -> str:
    """Le o CNPJ do prestador (<prest><CNPJ>...</CNPJ></prest>) do XML da NFS-e."""
    raiz = ET.fromstring(xml_texto)
    for elem in raiz.iter():
        if elem.tag.split("}")[-1] == "prest":
            for filho in elem.iter():
                if filho.tag.split("}")[-1] == "CNPJ" and filho.text:
                    return filho.text.strip()
    raise ValueError("Tag <prest><CNPJ> nao encontrada no XML")


def dentro_do_periodo(data_emissao: datetime, inicio: datetime, fim: datetime) -> bool:
    return inicio <= data_emissao <= fim


def salvar_xml(xml_texto: str, chave_acesso: str, data_emissao: datetime, pasta_destino: Path) -> bool:
    """
    Salva o XML na subpasta do mes de emissao. Retorna True se salvou,
    False se o arquivo ja existia (pulado).
    """
    mes = f"{data_emissao.month:02d}"
    pasta_mes = pasta_destino / mes
    pasta_mes.mkdir(parents=True, exist_ok=True)

    caminho_arquivo = pasta_mes / f"{chave_acesso}.xml"
    if caminho_arquivo.exists():
        return False

    caminho_arquivo.write_text(xml_texto, encoding="utf-8")
    return True


# ==========================================================================
# Fluxo principal de download
# ==========================================================================

def executar_download(config: Configuracao, thumbprint: str, descricao_cert: str) -> None:
    if not Path(CURL_EXE).exists():
        raise ConfigError([f"curl.exe nao encontrado em: {CURL_EXE}. Este Windows pode estar desatualizado."])

    try:
        config.pasta_destino.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError([
            f"Nao foi possivel acessar/criar a pasta de destino: {config.pasta_destino}",
            f"Detalhe: {exc}",
            "Verifique se a unidade de rede esta conectada e se o caminho no config.ini esta correto.",
        ])

    url_base = URL_BASE_PRODUCAO_RESTRITA if config.usar_producao_restrita else URL_BASE_PRODUCAO
    log.info("Cliente: %s (CNPJ %s)", config.nome_cliente, config.cnpj)
    log.info("Ambiente: %s", "producao restrita (homologacao)" if config.usar_producao_restrita else "producao oficial")
    log.info("Certificado: %s (thumbprint %s)", descricao_cert, thumbprint)
    log.info("Periodo de busca (dhEmi): %s a %s", config.data_inicio.date(), config.data_fim.date())
    log.info("Destino: %s", config.pasta_destino)

    nsu_atual = NSU_INICIAL
    contagem_por_mes = {}
    total_salvos = 0
    total_pulados = 0
    total_fora_periodo = 0
    total_nao_nfse = 0
    total_recebidas = 0
    total_erros = 0

    while True:
        url = f"{url_base}/DFe/{nsu_atual}?cnpjConsulta={config.cnpj}&lote=true"
        log.info("Consultando NSU >= %s ...", nsu_atual)

        try:
            status, corpo = chamar_com_retry(url, thumbprint)
        except Exception as exc:
            log.error("Falha definitiva ao consultar NSU %s: %s. Abortando.", nsu_atual, exc)
            break

        try:
            payload = json.loads(corpo) if corpo else {}
        except json.JSONDecodeError:
            log.error("Resposta nao-JSON (HTTP %d) ao consultar NSU %s: %s", status, nsu_atual, corpo[:300])
            break

        status_processamento = payload.get("StatusProcessamento")

        if status_processamento == "NENHUM_DOCUMENTO_LOCALIZADO":
            log.info("Nenhum documento a partir do NSU %s. Fim da paginacao.", nsu_atual)
            break

        if status_processamento == "REJEICAO":
            for erro in payload.get("Erros") or []:
                log.error("Rejeicao da API: [%s] %s", erro.get("Codigo"), erro.get("Descricao"))
            log.error("Abortando devido a rejeicao da API.")
            break

        if status_processamento != "DOCUMENTOS_LOCALIZADOS":
            log.error("Resposta inesperada (HTTP %d, StatusProcessamento=%s): %s", status, status_processamento, corpo[:500])
            break

        documentos = payload.get("LoteDFe") or []
        if not documentos:
            log.info("Lote vazio. Fim da paginacao.")
            break

        maior_nsu = nsu_atual
        for doc in documentos:
            nsu = doc.get("NSU")
            chave_acesso = doc.get("ChaveAcesso")
            conteudo_b64 = doc.get("ArquivoXml")

            if nsu is not None:
                try:
                    maior_nsu = max(maior_nsu, int(nsu))
                except (TypeError, ValueError):
                    pass

            if doc.get("TipoDocumento") != "NFSE":
                total_nao_nfse += 1
                continue

            if not chave_acesso or not conteudo_b64:
                log.warning("Documento com NSU=%s sem chave de acesso ou conteudo XML; pulando.", nsu)
                total_erros += 1
                continue

            try:
                xml_texto = descompactar_xml(conteudo_b64)
                data_emissao = extrair_data_emissao(xml_texto)
                cnpj_prestador = extrair_cnpj_prestador(xml_texto)
            except Exception as exc:
                log.error("Erro ao processar documento NSU=%s chave=%s: %s", nsu, chave_acesso, exc)
                total_erros += 1
                continue

            if cnpj_prestador != config.cnpj:
                total_recebidas += 1
                continue

            if not dentro_do_periodo(data_emissao, config.data_inicio, config.data_fim):
                total_fora_periodo += 1
                continue

            try:
                salvo = salvar_xml(xml_texto, chave_acesso, data_emissao, config.pasta_destino)
            except OSError as exc:
                log.error("Erro ao salvar XML chave=%s: %s", chave_acesso, exc)
                total_erros += 1
                continue

            mes = f"{data_emissao.month:02d}"
            if salvo:
                total_salvos += 1
                contagem_por_mes[mes] = contagem_por_mes.get(mes, 0) + 1
                log.info("Salvo: mes=%s chave=%s", mes, chave_acesso)
            else:
                total_pulados += 1

        if len(documentos) < 50:
            log.info("Lote com menos de 50 documentos; fim da paginacao.")
            break

        if maior_nsu == nsu_atual:
            log.warning("NSU nao avancou (maior_nsu=%s); interrompendo para evitar loop infinito.", maior_nsu)
            break

        nsu_atual = maior_nsu
        time.sleep(1)  # pequena pausa entre paginas para evitar HTTP 429

    log.info("==================== RESUMO ====================")
    for mes in sorted(contagem_por_mes):
        log.info("Mes %s: %d nota(s) nova(s) salva(s)", mes, contagem_por_mes[mes])
    log.info("Total salvos agora: %d", total_salvos)
    log.info("Total ja existentes (pulados): %d", total_pulados)
    log.info("Total fora do periodo configurado: %d", total_fora_periodo)
    log.info("Total ignorado (eventos/DPS/outros, nao-NFSe): %d", total_nao_nfse)
    log.info("Total ignorado (nota recebida, cliente e tomador): %d", total_recebidas)
    log.info("Total com erro: %d", total_erros)
    log.info("=================================================")


# ==========================================================================
# Ponto de entrada
# ==========================================================================

def aguardar_saida() -> None:
    if getattr(sys, "frozen", False):
        try:
            input("\nPressione ENTER para fechar esta janela...")
        except EOFError:
            pass


def main() -> None:
    try:
        if not caminho_config().exists():
            criar_config_padrao()
            print("=" * 72)
            print(f"Arquivo de configuracao criado: {caminho_config()}")
            print()
            print("Abra esse arquivo com o Bloco de Notas, preencha os dados do")
            print("cliente (nome, CNPJ, periodo e pasta de destino) e rode este")
            print("programa de novo.")
            print("=" * 72)
            return

        config = carregar_configuracao()
        thumbprint, descricao_cert = localizar_certificado(config.cnpj, config.thumbprint_manual)
        executar_download(config, thumbprint, descricao_cert)

    except ConfigError as exc:
        print("=" * 72)
        print("Nao foi possivel continuar por causa do(s) problema(s) abaixo:")
        print("=" * 72)
        for msg in exc.mensagens:
            print(f"- {msg}")
        print("=" * 72)
        print(f"Corrija o arquivo {NOME_ARQUIVO_CONFIG} e rode o programa de novo.")

    except Exception:
        print("=" * 72)
        print("Ocorreu um erro inesperado:")
        print("=" * 72)
        traceback.print_exc()
        print("=" * 72)
        print("Copie esta mensagem e envie para quem cuida do sistema.")

    finally:
        aguardar_saida()


if __name__ == "__main__":
    main()
