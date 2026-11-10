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

import argparse
import base64
import configparser
import gzip
import io
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
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
MAX_TENTATIVAS = 6
BACKOFF_SEGUNDOS = 2        # base da espera apos HTTP 429 (dobra a cada tentativa)
BACKOFF_REDE_SEGUNDOS = 3   # base da espera apos falha de rede (3, 6, 12, 24, 48 s)
LIMITE_CHECAGEM_PASTA = 15  # segundos: tempo maximo para saber se uma pasta/unidade de rede responde

# A chave de acesso da NFS-e tem exatamente 50 digitos. Como ela vira nome de arquivo,
# qualquer outra coisa (ex.: "..\\..\\x") e rejeitada antes de tocar no disco.
_PADRAO_CHAVE = re.compile(r"[0-9]{50}")  # [0-9] e nao \d: \d aceita digitos de outros alfabetos

VERSAO = "2.0.0"

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

; Existem DOIS casos. Veja qual e o do seu cliente:
;
; CASO 1 - O CLIENTE TEM O PROPRIO CERTIFICADO (e-CNPJ A1), instalado
;          neste computador, no usuario do Windows que roda o programa.
;          --> Deixe "thumbprint" EM BRANCO. O programa procura sozinho,
;              no repositorio de certificados do Windows, o certificado
;              cujo CNPJ bate com o informado acima em [CLIENTE] -> cnpj.
;
; CASO 2 - ACESSO POR PROCURACAO: o cliente NAO tem certificado e deu
;          procuracao eletronica ao CPF ou CNPJ do SEU certificado.
;          --> Preencha "thumbprint" com o do SEU certificado (o do
;              contador, e nao o do cliente). Em [CLIENTE] -> cnpj
;              continua o CNPJ do CLIENTE.
;
; Como achar o thumbprint (impressao digital) de um certificado:
;   Win+R > certmgr.msc > Pessoal > Certificados > de dois cliques no
;   certificado > aba "Detalhes" > campo "Impressao digital". Copie os
;   40 caracteres e cole aqui (espacos sao ignorados).
;
; Tambem preencha o thumbprint se o programa avisar que encontrou mais
; de um certificado valido para o mesmo CNPJ.
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


[SAIDA]

; O que baixar de cada nota:
;   xml   = so o arquivo XML (o documento oficial da nota)      [padrao]
;   pdf   = so o PDF (DANFSe), gerado pelo programa a partir do XML
;   ambos = o XML e o PDF
;
; O PDF e uma representacao grafica gerada pelo proprio programa, seguindo
; o layout da Nota Tecnica 008/2026 -- nao e baixado da Receita (a API de
; PDF da Receita foi desativada). O documento com validade juridica
; continua sendo o XML.
formato = xml


[DESTINO]

; Pasta onde os arquivos serao salvos. Use o caminho completo, como aparece
; na barra de enderecos do Windows Explorer. O programa cria sozinho,
; dentro dessa pasta, uma subpasta para cada mes (01, 02, 03...).
;
; Exemplo:
;   pasta = Z:\\FISCAL\\2026\\NFSE\\NOME DO CLIENTE\\Prestacao
pasta =

; (Opcional) Pasta separada para os PDFs. Se ficar em branco, os PDFs vao
; para a mesma pasta acima, ao lado dos XMLs.
pasta_pdf =


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


class CertificadoNaoEncontrado(ConfigError):
    """Nenhum certificado com o CNPJ informado esta instalado neste computador."""


@dataclass
class Configuracao:
    nome_cliente: str
    cnpj: str
    thumbprint_manual: str
    data_inicio: datetime
    data_fim: datetime
    pasta_destino: Path
    pasta_pdf: Path
    formato: str
    usar_producao_restrita: bool


# ==========================================================================
# Pasta de dados: config.ini (um cliente) ou clientes.csv/lote.ini, estado, logs
# e relatorios (lote). Por padrao fica ao lado do .exe/.py; pode ser outra pasta
# (--dados ou variavel de ambiente NFSE_DADOS), para manter dados de clientes
# fora da pasta do codigo.
# ==========================================================================

_PASTA_DADOS = None


def definir_pasta_dados(caminho) -> None:
    global _PASTA_DADOS
    _PASTA_DADOS = Path(caminho).expanduser().resolve() if caminho else None


def pasta_base() -> Path:
    if _PASTA_DADOS is not None:
        return _PASTA_DADOS
    ambiente = os.environ.get("NFSE_DADOS", "").strip()
    if ambiente:
        return Path(ambiente).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def validar_pasta_dados() -> Path:
    base = pasta_base()
    if not base.is_dir():
        raise ConfigError([f"A pasta de dados nao existe: {base}",
                           "Confira o parametro --dados ou a variavel de ambiente NFSE_DADOS."])
    return base


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
    pasta_pdf_txt = obter("DESTINO", "pasta_pdf", obrigatorio=False)
    pasta_pdf = Path(pasta_pdf_txt) if pasta_pdf_txt else pasta_destino

    formato = obter("SAIDA", "formato", obrigatorio=False, padrao="xml").strip().lower()
    if formato not in ("xml", "pdf", "ambos"):
        erros.append(f"[SAIDA] formato = '{formato}' invalido. Use 'xml', 'pdf' ou 'ambos'.")

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
        pasta_pdf=pasta_pdf,
        formato=formato,
        usar_producao_restrita=usar_producao_restrita,
    )


# ==========================================================================
# Localizacao do certificado no Windows Cert Store (por CNPJ ou thumbprint)
# ==========================================================================

def validar_thumbprint_manual(thumbprint: str) -> tuple:
    """
    Confere, no Cert Store do usuario, o thumbprint informado no config.ini
    (usado no acesso por procuracao). Evita um erro obscuro do curl.exe mais
    adiante quando o thumbprint esta errado, vencido ou sem chave privada.
    """
    comando_ps = (
        f"Get-ChildItem Cert:\\CurrentUser\\My\\{thumbprint} -ErrorAction SilentlyContinue | "
        f"ForEach-Object {{ \"$($_.NotAfter.ToString('yyyy-MM-dd'))|$($_.HasPrivateKey)|$($_.Subject)\" }}"
    )
    resultado = subprocess.run(
        [POWERSHELL_EXE, "-NoProfile", "-Command", comando_ps],
        capture_output=True, text=True, timeout=30,
    )
    linha = next((l.strip() for l in resultado.stdout.splitlines() if l.strip()), "")
    partes = linha.split("|", 2)

    if len(partes) != 3:
        raise ConfigError([
            f"O certificado com thumbprint {thumbprint} nao foi encontrado neste computador.",
            "Confira se o thumbprint em [CERTIFICADO] esta correto e se o certificado esta",
            "instalado no Windows do usuario que esta rodando o programa.",
        ])

    validade_txt, tem_chave, subject = partes
    if tem_chave.strip().lower() != "true":
        raise ConfigError([
            f"O certificado {subject} esta instalado, mas sem a chave privada.",
            "Reinstale o certificado A1 (arquivo .pfx) neste computador.",
        ])

    try:
        validade = datetime.strptime(validade_txt, "%Y-%m-%d")
    except ValueError:
        validade = None
    if validade and validade < datetime.now():
        raise ConfigError([
            f"O certificado {subject} esta vencido desde {validade_txt}.",
            "Providencie a renovacao ou use outro certificado.",
        ])

    return thumbprint, f"{subject} (valido ate {validade_txt}; thumbprint informado no config.ini)"


def localizar_certificado(cnpj: str, thumbprint_manual: str) -> tuple:
    """Retorna (thumbprint, descricao) do certificado a ser usado nas chamadas."""
    if thumbprint_manual:
        return validar_thumbprint_manual(thumbprint_manual)

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
        raise CertificadoNaoEncontrado([
            f"Nenhum certificado digital com o CNPJ {cnpj} foi encontrado neste computador.",
            "- Se o cliente tem certificado proprio: instale o certificado A1 dele no Windows",
            "  deste usuario (o mesmo usado para acessar o e-CAC, por exemplo).",
            "- Se o acesso e por PROCURACAO (o certificado e o seu, e nao o do cliente):",
            "  preencha [CERTIFICADO] thumbprint no config.ini com o thumbprint do SEU",
            "  certificado. Para achar: Win+R > certmgr.msc > Pessoal > Certificados >",
            "  abra o certificado > aba Detalhes > Impressao digital.",
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
        "-sS",
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
            time.sleep(BACKOFF_REDE_SEGUNDOS * (2 ** (tentativa - 1)))

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


_INDICE_PASTAS = {}


def _nomes_na_pasta(pasta: Path) -> set:
    """Nomes (em minusculas) dos arquivos de uma pasta, lidos uma unica vez por execucao."""
    chave = str(pasta)
    if chave not in _INDICE_PASTAS:
        try:
            _INDICE_PASTAS[chave] = {n.lower() for n in os.listdir(pasta)}
        except OSError:
            _INDICE_PASTAS[chave] = set()
    return _INDICE_PASTAS[chave]


def chave_valida(chave) -> bool:
    """True se `chave` e uma chave de acesso de NFS-e: exatamente 50 digitos."""
    return isinstance(chave, str) and _PADRAO_CHAVE.fullmatch(chave) is not None


def _exigir_chave(chave) -> None:
    if not chave_valida(chave):
        raise ValueError(f"chave de acesso invalida (esperados 50 digitos): {str(chave)[:60]!r}")


def com_limite_de_tempo(funcao, argumento, limite: float, padrao=False):
    """
    Roda `funcao(argumento)` e devolve o resultado, ou `padrao` se passar de `limite` segundos
    (ou se der erro). Uma unidade de rede fora do ar pode travar o Windows por ~40 s numa unica
    consulta; a thread e "daemon" para nao segurar o encerramento do programa.
    """
    resultado = [padrao]

    def trabalho():
        try:
            resultado[0] = funcao(argumento)
        except Exception:
            resultado[0] = padrao

    fio = threading.Thread(target=trabalho, daemon=True)
    fio.start()
    fio.join(limite)
    return padrao if fio.is_alive() else resultado[0]


def pasta_acessivel(pasta, limite: float = None) -> bool:
    """True se a pasta existe e responde dentro do limite de tempo."""
    return com_limite_de_tempo(lambda p: Path(p).exists(), pasta,
                               LIMITE_CHECAGEM_PASTA if limite is None else limite)


def ja_existe(pasta_mes: Path, chave_acesso: str, extensao: str) -> bool:
    """
    True se a pasta ja tem arquivo dessa nota: nome igual a chave, ou chave seguida de
    sufixo (ex.: '<chave> - 02.01.xml'), para nao duplicar o que a equipe ja salvou.
    """
    chave = chave_acesso.lower()
    return any(n.startswith(chave) and n.endswith(extensao) for n in _nomes_na_pasta(pasta_mes))


def salvar_xml(xml_texto: str, chave_acesso: str, data_emissao: datetime, pasta_destino: Path) -> bool:
    """
    Salva o XML na subpasta do mes de emissao. Retorna True se salvou,
    False se a nota ja existia na pasta (pulada).
    """
    _exigir_chave(chave_acesso)
    pasta_mes = pasta_destino / f"{data_emissao.month:02d}"
    if ja_existe(pasta_mes, chave_acesso, ".xml"):
        return False

    pasta_mes.mkdir(parents=True, exist_ok=True)
    nome = f"{chave_acesso}.xml"
    (pasta_mes / nome).write_text(xml_texto, encoding="utf-8")
    _nomes_na_pasta(pasta_mes).add(nome.lower())
    return True


def carregar_gerador_pdf():
    """Importa o gerador de PDF so quando o PDF foi pedido (o modo XML nao exige a reportlab)."""
    try:
        import danfse
    except ImportError as exc:
        raise ConfigError([
            "Para gerar PDF e preciso a biblioteca 'reportlab', que nao esta instalada.",
            "Instale com:  pip install reportlab",
            f"Detalhe: {exc}",
        ])
    danfse.registrar_fontes()
    return danfse


def salvar_pdf(gerador, xml_texto: str, chave_acesso: str, data_emissao: datetime, pasta_destino: Path) -> bool:
    """
    Gera o DANFSe (PDF) da nota na subpasta do mes de emissao. Retorna True se gerou,
    False se ja existia um PDF dessa nota (pulado). Grava num arquivo temporario e so entao
    renomeia, para uma falha no meio nunca deixar um PDF incompleto que seria pulado
    nas execucoes seguintes.
    """
    _exigir_chave(chave_acesso)
    pasta_mes = pasta_destino / f"{data_emissao.month:02d}"
    if ja_existe(pasta_mes, chave_acesso, ".pdf"):
        return False

    pasta_mes.mkdir(parents=True, exist_ok=True)
    nome = f"{chave_acesso}.pdf"
    temporario = pasta_mes / f"{nome}.tmp"
    try:
        gerador.gerar_pdf(xml_texto, str(temporario))
        temporario.replace(pasta_mes / nome)
    finally:
        if temporario.exists():
            temporario.unlink()
    _nomes_na_pasta(pasta_mes).add(nome.lower())
    return True


# ==========================================================================
# Fluxo principal de download
# ==========================================================================

@dataclass
class ResultadoDownload:
    """Resultado do download de um cliente (usado pelo modo em lote para o relatorio)."""
    xml_novos: int = 0
    xml_pulados: int = 0
    pdf_novos: int = 0
    pdf_pulados: int = 0
    fora_periodo: int = 0
    nao_nfse: int = 0
    recebidas: int = 0
    erros: int = 0
    docs_vistos: int = 0
    nsu_inicial: int = 0
    ultimo_nsu: int = 0
    completo: bool = False  # True so se a paginacao chegou ao fim sem abortar
    erro: str = ""
    por_mes: dict = field(default_factory=dict)  # mes -> [xml novos, pdf novos]


def executar_download(config: Configuracao, thumbprint: str, descricao_cert: str,
                      nsu_inicial: int = NSU_INICIAL, criar_pasta_base: bool = True) -> ResultadoDownload:
    """
    Baixa e grava as NFS-e emitidas pelo cliente no periodo. Problemas de configuracao
    (pasta inacessivel, falta de curl.exe/reportlab) levantam ConfigError; falhas durante a
    paginacao encerram o loop e ficam em `erro` (com `completo` False).
    `criar_pasta_base=False` exige que a pasta de destino ja exista (evita criar pastas
    novas por causa de um caminho digitado errado).
    """
    if not Path(CURL_EXE).exists():
        raise ConfigError([f"curl.exe nao encontrado em: {CURL_EXE}. Este Windows pode estar desatualizado."])

    quer_xml = config.formato in ("xml", "ambos")
    quer_pdf = config.formato in ("pdf", "ambos")

    gerador_pdf = carregar_gerador_pdf() if quer_pdf else None
    if gerador_pdf is not None and not gerador_pdf.FONTES_ORIGINAIS:
        log.warning("Fontes Arial/Microsoft Sans Serif nao encontradas em C:\\Windows\\Fonts; "
                    "o PDF usara Helvetica (layout levemente diferente do oficial).")

    for pasta in ([config.pasta_destino] if quer_xml else []) + ([config.pasta_pdf] if quer_pdf else []):
        if criar_pasta_base:
            try:
                pasta.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ConfigError([
                    f"Nao foi possivel acessar/criar a pasta de destino: {pasta}",
                    f"Detalhe: {exc}",
                    "Verifique se a unidade de rede esta conectada e se o caminho no config.ini esta correto.",
                ])
        elif not pasta_acessivel(pasta):
            raise ConfigError([
                f"A pasta de destino nao existe ou nao esta acessivel: {pasta}",
                "Confira o caminho na lista de clientes e se a unidade de rede esta conectada.",
            ])

    url_base = URL_BASE_PRODUCAO_RESTRITA if config.usar_producao_restrita else URL_BASE_PRODUCAO
    log.info("Cliente: %s (CNPJ %s)", config.nome_cliente, config.cnpj)
    log.info("Ambiente: %s", "producao restrita (homologacao)" if config.usar_producao_restrita else "producao oficial")
    log.info("Certificado: %s (thumbprint %s)", descricao_cert, thumbprint)
    log.info("Periodo de busca (dhEmi): %s a %s", config.data_inicio.date(), config.data_fim.date())
    log.info("Formato de saida: %s", config.formato)
    if quer_xml:
        log.info("Destino dos XMLs: %s", config.pasta_destino)
    if quer_pdf:
        log.info("Destino dos PDFs: %s", config.pasta_pdf)
    if nsu_inicial:
        log.info("Retomando do NSU %s (periodo ja coberto numa execucao anterior)", nsu_inicial)

    res = ResultadoDownload(nsu_inicial=nsu_inicial, ultimo_nsu=nsu_inicial)
    nsu_atual = nsu_inicial

    while True:
        url = f"{url_base}/DFe/{nsu_atual}?cnpjConsulta={config.cnpj}&lote=true"
        log.info("Consultando NSU >= %s ...", nsu_atual)

        try:
            status, corpo = chamar_com_retry(url, thumbprint)
        except Exception as exc:
            res.erro = f"falha ao consultar o ADN (NSU {nsu_atual}): {exc}"
            log.error("Falha definitiva ao consultar NSU %s: %s. Abortando.", nsu_atual, exc)
            break

        try:
            payload = json.loads(corpo) if corpo else {}
        except json.JSONDecodeError:
            res.erro = f"resposta invalida do ADN (HTTP {status})"
            log.error("Resposta nao-JSON (HTTP %d) ao consultar NSU %s: %s", status, nsu_atual, corpo[:300])
            break

        status_processamento = payload.get("StatusProcessamento")

        if status_processamento == "NENHUM_DOCUMENTO_LOCALIZADO":
            log.info("Nenhum documento a partir do NSU %s. Fim da paginacao.", nsu_atual)
            res.completo = True
            break

        if status_processamento == "REJEICAO":
            mensagens = []
            for erro in payload.get("Erros") or []:
                log.error("Rejeicao da API: [%s] %s", erro.get("Codigo"), erro.get("Descricao"))
                mensagens.append(f"[{erro.get('Codigo')}] {erro.get('Descricao')}")
            res.erro = "rejeicao da API: " + "; ".join(mensagens)
            log.error("Abortando devido a rejeicao da API.")
            break

        if status_processamento != "DOCUMENTOS_LOCALIZADOS":
            res.erro = f"resposta inesperada do ADN (HTTP {status}, {status_processamento})"
            log.error("Resposta inesperada (HTTP %d, StatusProcessamento=%s): %s", status, status_processamento, corpo[:500])
            break

        documentos = payload.get("LoteDFe") or []
        if not documentos:
            log.info("Lote vazio. Fim da paginacao.")
            res.completo = True
            break

        res.docs_vistos += len(documentos)
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
                res.nao_nfse += 1
                continue

            if not chave_acesso or not conteudo_b64:
                log.warning("Documento com NSU=%s sem chave de acesso ou conteudo XML; pulando.", nsu)
                res.erros += 1
                continue

            if not chave_valida(chave_acesso):
                log.error("Documento com NSU=%s tem chave de acesso invalida (%r); pulando.", nsu, str(chave_acesso)[:60])
                res.erros += 1
                continue

            try:
                xml_texto = descompactar_xml(conteudo_b64)
                data_emissao = extrair_data_emissao(xml_texto)
                cnpj_prestador = extrair_cnpj_prestador(xml_texto)
            except Exception as exc:
                log.error("Erro ao processar documento NSU=%s chave=%s: %s", nsu, chave_acesso, exc)
                res.erros += 1
                continue

            if cnpj_prestador != config.cnpj:
                res.recebidas += 1
                continue

            if not dentro_do_periodo(data_emissao, config.data_inicio, config.data_fim):
                res.fora_periodo += 1
                continue

            mes = f"{data_emissao.month:02d}"
            contagem = res.por_mes.setdefault(mes, [0, 0])
            novos = []

            if quer_xml:
                try:
                    if salvar_xml(xml_texto, chave_acesso, data_emissao, config.pasta_destino):
                        res.xml_novos += 1
                        contagem[0] += 1
                        novos.append("xml")
                    else:
                        res.xml_pulados += 1
                except OSError as exc:
                    log.error("Erro ao salvar XML chave=%s: %s", chave_acesso, exc)
                    res.erros += 1

            if gerador_pdf is not None:
                try:
                    if salvar_pdf(gerador_pdf, xml_texto, chave_acesso, data_emissao, config.pasta_pdf):
                        res.pdf_novos += 1
                        contagem[1] += 1
                        novos.append("pdf")
                    else:
                        res.pdf_pulados += 1
                except Exception as exc:
                    log.error("Erro ao gerar PDF chave=%s: %s", chave_acesso, exc)
                    res.erros += 1

            if novos:
                log.info("Salvo: mes=%s chave=%s (%s)", mes, chave_acesso, "+".join(novos))

        res.ultimo_nsu = max(res.ultimo_nsu, maior_nsu)

        if len(documentos) < 50:
            log.info("Lote com menos de 50 documentos; fim da paginacao.")
            res.completo = True
            break

        if maior_nsu == nsu_atual:
            log.warning("NSU nao avancou (maior_nsu=%s); interrompendo para evitar loop infinito.", maior_nsu)
            res.erro = "a paginacao nao avancou"
            break

        nsu_atual = maior_nsu
        time.sleep(1)  # pequena pausa entre paginas para evitar HTTP 429

    log.info("==================== RESUMO ====================")
    for mes in sorted(res.por_mes):
        xml_novos, pdf_novos = res.por_mes[mes]
        if xml_novos or pdf_novos:
            partes = ([f"{xml_novos} XML"] if quer_xml else []) + ([f"{pdf_novos} PDF"] if quer_pdf else [])
            log.info("Mes %s: %s novo(s)", mes, " e ".join(partes))
    if quer_xml:
        log.info("XMLs salvos agora: %d", res.xml_novos)
        log.info("XMLs ja existentes (pulados): %d", res.xml_pulados)
    if quer_pdf:
        log.info("PDFs gerados agora: %d", res.pdf_novos)
        log.info("PDFs ja existentes (pulados): %d", res.pdf_pulados)
    log.info("Total fora do periodo configurado: %d", res.fora_periodo)
    log.info("Total ignorado (eventos/DPS/outros, nao-NFSe): %d", res.nao_nfse)
    log.info("Total ignorado (nota recebida, cliente e tomador): %d", res.recebidas)
    log.info("Total com erro: %d", res.erros)
    log.info("=================================================")
    return res


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
    parser = argparse.ArgumentParser(description="Baixa as NFS-e de UM cliente, conforme o config.ini.")
    parser.add_argument("--dados", help="pasta do config.ini (padrao: ao lado do programa, ou a variavel NFSE_DADOS)")
    parser.add_argument("--versao", action="version", version=f"nfse-downloader {VERSAO}")
    args = parser.parse_args()
    try:
        definir_pasta_dados(args.dados)
        validar_pasta_dados()
        log.info("NFS-e downloader v%s", VERSAO)
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
