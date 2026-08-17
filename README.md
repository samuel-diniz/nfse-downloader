# NFS-e ADN — Download em lote via API oficial

Script Python para **download automatizado, paginado e idempotente** de
NFS-e emitidas pelo contribuinte, direto da API oficial do **Ambiente de
Distribuição Nacional (ADN)** da NFS-e Nacional.

Autenticação por **mTLS com certificado digital A1** lido diretamente do
**Windows Cert Store** — a chave privada nunca sai do repositório protegido
do sistema operacional (não há `.pfx` solto no disco, não há senha em texto
claro em lugar nenhum).

## Motivação

Escritórios de contabilidade que fecham a apuração mensal de serviços
prestados/tomados precisam baixar dezenas ou centenas de NFS-e por
cliente todo mês. Fazer isso manualmente (portal a portal, cliente a
cliente) é lento, sujeito a erro e não escala. Este script automatiza o
processo para um contribuinte, salvando os XMLs organizados por
`ano/mês/{chave_de_acesso}.xml`.

## Como funciona

1. Lê configuração (CNPJ, thumbprint do certificado, pasta destino) de
   variáveis de ambiente carregadas de um arquivo `.env` local.
2. Pagina a API do ADN por NSU, começando de 0 (ou de um NSU salvo).
3. Para cada documento retornado:
   - Ignora eventos, DPS e outros tipos que não são NFS-e.
   - Decodifica Base64 e descompacta o GZip do payload.
   - Lê `<dhEmi>` para descobrir o mês de emissão.
   - Lê `<prest><CNPJ>` para distinguir notas emitidas (prestação) de notas
     recebidas — o ADN retorna os dois tipos no mesmo lote de distribuição.
   - Salva o XML em `{PASTA_BASE}/{mês:02d}/{chave_acesso}.xml`.
4. Retry com backoff exponencial em caso de HTTP 429 (rate limit) ou 5xx.
5. Idempotente: reexecuções pulam arquivos já salvos (nome = chave de acesso).

## Decisões técnicas

### Por que `curl.exe` do Windows em vez de `requests`?

A biblioteca `requests` (via OpenSSL) não consegue ler certificados do
repositório do Windows — exige um `.pfx` solto no disco + senha em texto
claro no código ou em variável de ambiente.

O `curl.exe` nativo do Windows 10/11 usa o backend **Schannel**
(CryptoAPI), que consegue referenciar o certificado pelo thumbprint direto
no Cert Store, sem exportar a chave privada em momento nenhum.

**Trade-off:** perde-se portabilidade Linux, ganha-se um modelo de
segurança muito mais forte (chave privada permanece protegida pelo SO).

### Por que paginar por NSU e não por data de emissão?

A API do ADN pagina exclusivamente por **NSU** (Número Sequencial Único).
A filtragem por data é feita no cliente, após descompactar o XML e ler
`<dhEmi>`. Isso é limitação da própria API, não escolha do script.

### Por que HTTP 404 nem sempre é erro?

A API responde HTTP 404 tanto para "fim da paginação"
(`StatusProcessamento = NENHUM_DOCUMENTO_LOCALIZADO`) quanto para outros
erros. O script sempre inspeciona o corpo da resposta em vez de confiar
apenas no código HTTP.

### Por que filtrar `cnpj_prestador == cnpj_contribuinte`?

A API de distribuição retorna documentos onde o contribuinte figura tanto
como **prestador** (nota emitida por ele) quanto como **tomador** (nota
recebida). Como o objetivo aqui é popular a pasta de "Prestação", filtra-se
pelo CNPJ do prestador extraído do próprio XML.

## Requisitos

- Windows 10 ou superior (para acesso ao Cert Store via Schannel).
- Python 3.11+.
- Certificado digital A1 instalado em `CurrentUser\My`.
- `curl.exe` em `C:\Windows\System32\` (padrão em Windows 10+).

## Instalação e configuração

```bash
git clone https://github.com/seu-usuario/nfse-adn-downloader.git
cd nfse-adn-downloader

python -m venv .venv
.venv\Scripts\activate

pip install -r requirements.txt

copy .env.example .env
# edite o .env com CNPJ, thumbprint e pasta destino
```

## Execução

Usando os valores do `.env`:

```bash
python baixar_nfse.py
```

Sobrescrevendo o período pela linha de comando:

```bash
python baixar_nfse.py --data-inicio 2026-01-01 --data-fim 2026-03-31
```

## Estrutura de saída

```
{PASTA_BASE_DESTINO}/
    01/
        {chave_acesso_1}.xml
        {chave_acesso_2}.xml
    02/
        {chave_acesso_3}.xml
    ...
```

## Stack

Python 3.11+ · `python-dotenv` · biblioteca padrão (`subprocess`, `gzip`,
`base64`, `xml.etree.ElementTree`, `argparse`, `logging`, `pathlib`) ·
`curl.exe` do Windows (Schannel) para mTLS.

## Licença

MIT.
