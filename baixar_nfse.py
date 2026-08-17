"""
Download em lote de NFS-e (NFS-e Nacional / ADN) via API oficial, com
autenticacao mTLS por certificado digital A1 ja instalado no repositorio
de certificados do Windows (Cert Store).

------------------------------------------------------------------------
COMO USAR
------------------------------------------------------------------------
1. Instale as dependencias:

       pip install -r requirements.txt

2. Copie .env.example para .env e preencha com os dados do contribuinte:

       copy .env.example .env
       (edite o .env)

   Variaveis obrigatorias:
       CERT_THUMBPRINT       Thumbprint (SHA-1) do certificado A1 instalado
                             em CurrentUser\\My do Windows Cert Store.
       CNPJ_CONTRIBUINTE     CNPJ do contribuinte, somente digitos.
       PASTA_BASE_DESTINO    Pasta destino para os XMLs. Pode conter o
                             marcador {ano}, que sera substituido pelo
                             valor de ANO_DESTINO.

3. Rode o script:

       python baixar_nfse.py

   Nao ha senha para digitar: o certificado ja esta instalado no Windows
   e a chave privada nunca sai do repositorio do sistema operacional. A
   chamada e feita via "curl.exe --cert CurrentUser\\My\\<thumbprint>",
   que usa o backend Schannel do Windows (CryptoAPI) para autenticacao
   mTLS sem exportar a chave em nenhum momento.

4. Voce pode rodar o script quantas vezes quiser: notas ja baixadas
   (mesmo nome de arquivo = mesma chave de acesso) sao puladas.

------------------------------------------------------------------------
SOBRE O ENDPOINT E O SCHEMA DA API
------------------------------------------------------------------------
Endpoint: GET {base}/DFe/{NSU}?cnpjConsulta={cnpj}&lote=true
  (o CNPJ NAO entra no path da URL -- ele e validado contra o CNPJ do
  certificado usado na conexao; "cnpjConsulta" so e necessario quando se
  quer consultar um CNPJ diferente do CNPJ raiz do certificado).

Resposta (schema LoteDistribuicaoNSUResponse, confirmado via swagger.json
do proprio ADN):
  - StatusProcessamento: "DOCUMENTOS_LOCALIZADOS" | "NENHUM_DOCUMENTO_LOCALIZADO" | "REJEICAO"
  - LoteDFe: lista de documentos, cada um com NSU, ChaveAcesso, ArquivoXml
    (XML compactado em GZip + Base64), DataHoraGeracao
  - Erros / Alertas: listas de mensagens de processamento

A API responde HTTP 404 tanto para "fim da paginacao" (StatusProcessamento
NENHUM_DOCUMENTO_LOCALIZADO) quanto para outros erros -- por isso o corpo
da resposta e sempre inspecionado, nao so o codigo HTTP.
"""

import argparse
import base64
import gzip
import io
import json
import logging
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ==========================================================================
# CONFIGURACAO
# --------------------------------------------------------------------------
# Dados sensiveis (CNPJ, thumbprint do certificado, caminho de destino)
# ficam em .env -- esse arquivo NAO deve ser commitado.
# Ver .env.example para o formato esperado.
# ==========================================================================

# Certificado digital, ja instalado no repositorio do Windows (nao e mais
# um arquivo .pfx solto). Local: usuario atual, repositorio "Pessoal" (My).
CERT_STORE_LOCATION = "CurrentUser"
CERT_STORE_NAME = "MY"
CERT_THUMBPRINT = os.getenv("CERT_THUMBPRINT", "")

# Caminho do curl.exe nativo do Windows (backend Schannel, suporta
# certificados direto do Cert Store). Usar o caminho fixo evita pegar por
# engano outro "curl" no PATH (ex.: o do Git for Windows, que usa OpenSSL
# e NAO sabe ler certificados do repositorio do Windows).
CURL_EXE = os.getenv("CURL_EXE", r"C:\Windows\System32\curl.exe")

# CNPJ do contribuinte (somente digitos).
CNPJ_CONTRIBUINTE = os.getenv("CNPJ_CONTRIBUINTE", "")

# Periodo de busca (data de emissao, tag <dhEmi> do XML), formato AAAA-MM-DD.
DATA_INICIO = os.getenv("DATA_INICIO", "2026-01-01")
DATA_FIM = os.getenv("DATA_FIM", "2026-12-31")

# Ano usado no caminho de destino (substitui {ano} em PASTA_BASE_DESTINO).
ANO_DESTINO = os.getenv("ANO_DESTINO", "2026")

# Ambiente: True = producao restrita (homologacao), False = producao oficial.
USAR_PRODUCAO_RESTRITA = os.getenv("USAR_PRODUCAO_RESTRITA", "false").lower() == "true"

URL_BASE_PRODUCAO = "https://adn.nfse.gov.br/contribuintes"
URL_BASE_PRODUCAO_RESTRITA = "https://adn.producaorestrita.nfse.gov.br/contribuintes"

# Pasta base de destino. Pode conter o marcador {ano}, que sera substituido
# pelo valor de ANO_DESTINO. O script cria as subpastas de mes ("01", "02",
# ...) automaticamente dentro dela.
_pasta_template = os.getenv("PASTA_BASE_DESTINO", "")
PASTA_BASE_DESTINO = Path(_pasta_template.format(ano=ANO_DESTINO)) if _pasta_template else None

# NSU inicial da busca. 0 = comeca do inicio do historico disponivel.
NSU_INICIAL = int(os.getenv("NSU_INICIAL", "0"))

# Tentativas de retry por chamada HTTP, com backoff exponencial simples.
# So se aplica a falhas de rede/servidor -- HTTP 404 de "fim de lista" (ver
# acima) nao e retentado, e tratado como termino normal da paginacao.
MAX_TENTATIVAS = 3
BACKOFF_SEGUNDOS = 2

# ==========================================================================
# fim da configuracao
# ==========================================================================


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nfse")

MARCADOR_STATUS = "\n__HTTP_STATUS__:"


def chamar_api(url: str) -> tuple:
    """
    Faz GET via curl.exe usando o certificado do Windows Cert Store
    (mTLS/Schannel, sem senha, sem exportar a chave privada).
    Retorna (status_http, corpo_texto).
    """
    cert_ref = f"{CERT_STORE_LOCATION}\\{CERT_STORE_NAME}\\{CERT_THUMBPRINT}"
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


def chamar_com_retry(url: str) -> tuple:
    """
    Chama a API com algumas tentativas em caso de falha de rede/servidor
    ou de limite de requisicoes (HTTP 429), que a API do ADN aplica de
    forma bastante agressiva mesmo com poucas chamadas seguidas.
    """
    ultima_excecao = None
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            status, corpo = chamar_api(url)
            if status == 429:
                espera = BACKOFF_SEGUNDOS * (2 ** tentativa)
                log.warning(
                    "Tentativa %d/%d: HTTP 429 (limite de requisicoes) ao chamar %s -- aguardando %ds",
                    tentativa, MAX_TENTATIVAS, url, espera,
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


def descompactar_xml(conteudo_b64: str) -> str:
    """Decodifica Base64 e descompacta GZip, retornando o XML como texto."""
    dados_gzip = base64.b64decode(conteudo_b64)
    with gzip.GzipFile(fileobj=io.BytesIO(dados_gzip)) as f:
        return f.read().decode("utf-8")


def extrair_data_emissao(xml_texto: str) -> datetime:
    """Le a tag <dhEmi> do XML da NFS-e e retorna a data de emissao."""
    raiz = ET.fromstring(xml_texto)
    for elem in raiz.iter():
        tag_local = elem.tag.split("}")[-1]
        if tag_local == "dhEmi" and elem.text:
            texto_data = elem.text.strip()[:10]
            return datetime.strptime(texto_data, "%Y-%m-%d")
    raise ValueError("Tag <dhEmi> nao encontrada no XML")


def extrair_cnpj_prestador(xml_texto: str) -> str:
    """
    Le o CNPJ do prestador (<prest><CNPJ>...</CNPJ></prest>) do XML da NFS-e.
    A API de distribuicao retorna tanto notas emitidas pelo contribuinte
    (prestador) quanto notas onde ele figura como tomador (servico
    recebido de terceiros) -- so as emitidas por ele interessam para a
    pasta de Prestacao.
    """
    raiz = ET.fromstring(xml_texto)
    for elem in raiz.iter():
        if elem.tag.split("}")[-1] == "prest":
            for filho in elem.iter():
                if filho.tag.split("}")[-1] == "CNPJ" and filho.text:
                    return filho.text.strip()
    raise ValueError("Tag <prest><CNPJ> nao encontrada no XML")


def dentro_do_periodo(data_emissao: datetime, inicio: datetime, fim: datetime) -> bool:
    return inicio <= data_emissao <= fim


def salvar_xml(xml_texto: str, chave_acesso: str, data_emissao: datetime) -> bool:
    """
    Salva o XML na subpasta do mes de emissao. Retorna True se salvou,
    False se o arquivo ja existia (pulado).
    """
    mes = f"{data_emissao.month:02d}"
    pasta_mes = PASTA_BASE_DESTINO / mes
    pasta_mes.mkdir(parents=True, exist_ok=True)

    caminho_arquivo = pasta_mes / f"{chave_acesso}.xml"
    if caminho_arquivo.exists():
        return False

    caminho_arquivo.write_text(xml_texto, encoding="utf-8")
    return True


def main():
    parser = argparse.ArgumentParser(description="Download em lote de NFS-e via API do ADN")
    parser.add_argument("--data-inicio", default=DATA_INICIO, help="Data inicial (AAAA-MM-DD)")
    parser.add_argument("--data-fim", default=DATA_FIM, help="Data final (AAAA-MM-DD)")
    parser.add_argument("--cnpj", default=CNPJ_CONTRIBUINTE, help="CNPJ do contribuinte (somente digitos)")
    args = parser.parse_args()

    # Validacao das variaveis obrigatorias vindas do .env
    if not CERT_THUMBPRINT:
        log.error("CERT_THUMBPRINT nao configurado. Defina no arquivo .env (veja .env.example).")
        sys.exit(1)

    if PASTA_BASE_DESTINO is None:
        log.error("PASTA_BASE_DESTINO nao configurado. Defina no arquivo .env (veja .env.example).")
        sys.exit(1)

    cnpj = args.cnpj.strip()
    if not cnpj:
        log.error("CNPJ do contribuinte nao informado. Defina CNPJ_CONTRIBUINTE em .env ou passe --cnpj.")
        sys.exit(1)

    if not Path(CURL_EXE).exists():
        log.error("curl.exe nao encontrado em: %s", CURL_EXE)
        sys.exit(1)

    data_inicio = datetime.strptime(args.data_inicio, "%Y-%m-%d")
    data_fim = datetime.strptime(args.data_fim, "%Y-%m-%d")

    url_base = URL_BASE_PRODUCAO_RESTRITA if USAR_PRODUCAO_RESTRITA else URL_BASE_PRODUCAO
    log.info("Ambiente: %s", "producao restrita (homologacao)" if USAR_PRODUCAO_RESTRITA else "producao oficial")
    log.info("Certificado: %s\\%s\\%s", CERT_STORE_LOCATION, CERT_STORE_NAME, CERT_THUMBPRINT[:8] + "...")
    log.info("Periodo de busca (dhEmi): %s a %s", args.data_inicio, args.data_fim)
    log.info("Destino base: %s", PASTA_BASE_DESTINO)

    nsu_atual = NSU_INICIAL
    contagem_por_mes = {}
    total_salvos = 0
    total_pulados = 0
    total_fora_periodo = 0
    total_nao_nfse = 0
    total_recebidas = 0
    total_erros = 0

    while True:
        url = f"{url_base}/DFe/{nsu_atual}?cnpjConsulta={cnpj}&lote=true"
        log.info("Consultando NSU >= %s ...", nsu_atual)

        try:
            status, corpo = chamar_com_retry(url)
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

            # O lote de distribuicao tambem traz eventos (cancelamento,
            # confirmacao, etc.), DPS e outros tipos de documento, que tem
            # um XML diferente (sem a tag <dhEmi>). So processamos NFS-e.
            tipo_documento = doc.get("TipoDocumento")
            if tipo_documento != "NFSE":
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

            # A API tambem retorna notas onde o contribuinte figura como
            # tomador (servico recebido de terceiros). So interessam aqui
            # as notas efetivamente emitidas por ele (prestador).
            if cnpj_prestador != cnpj:
                total_recebidas += 1
                continue

            if not dentro_do_periodo(data_emissao, data_inicio, data_fim):
                total_fora_periodo += 1
                continue

            try:
                salvo = salvar_xml(xml_texto, chave_acesso, data_emissao)
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
    log.info("Total ignorado (nota recebida, contribuinte e tomador): %d", total_recebidas)
    log.info("Total com erro: %d", total_erros)
    log.info("=================================================")


if __name__ == "__main__":
    main()
