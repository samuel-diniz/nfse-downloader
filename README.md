# NFS-e ADN — Download em lote via API oficial

Programa para **download automatizado, paginado e idempotente** de NFS-e
emitidas por um contribuinte, direto da API oficial do **Ambiente de Dados
Nacional (ADN)** da NFS-e Nacional.

A autenticação é por **mTLS com certificado digital A1** lido diretamente do
**Windows Cert Store** — a chave privada nunca sai do repositório protegido do
sistema operacional (não há `.pfx` solto no disco e não há senha em lugar
nenhum).

Pode ser usado de duas formas:

- **`baixar_nfse.exe`** — executável pronto, sem precisar instalar Python.
  Pensado para equipes que não programam: preenche-se um `config.ini` e dá-se
  dois cliques.
- **`baixar_nfse.py`** — o código-fonte, para quem prefere rodar com Python ou
  adaptar.

## Motivação

Escritórios de contabilidade que fecham a apuração mensal de serviços
prestados precisam baixar dezenas ou centenas de NFS-e por cliente todo mês.
Fazer isso manualmente, cliente a cliente, é lento, sujeito a erro e não
escala. Este programa automatiza o processo, salvando os XMLs organizados por
mês de emissão, e pode ser reexecutado quantas vezes for preciso sem duplicar
nada.

## Uso rápido (executável)

O executável não fica no código-fonte: baixe o `.zip` da versão mais recente
na página de [Releases](https://github.com/samuel-diniz/nfse-downloader/releases),
extraia e siga os passos abaixo. Confira o SHA-256 informado nas notas da
Release (`certutil -hashfile <arquivo> SHA256`).

1. Execute `baixar_nfse.exe` uma vez. Ele cria um `config.ini` comentado ao
   lado dele (o modelo está em [`config.ini.example`](config.ini.example)).
2. Abra o `config.ini` no Bloco de Notas e preencha:
   - `[CLIENTE]` — nome e **CNPJ** (somente números);
   - `[PERIODO]` — `data_inicio` e `data_fim`, no formato `AAAA-MM-DD`;
   - `[DESTINO]` — pasta onde os XMLs serão salvos.
3. Execute `baixar_nfse.exe` de novo. Ao final, a janela espera ENTER para
   que o resumo possa ser lido.

Instruções em linguagem simples para quem vai operar o programa estão em
[`COMO_USAR.txt`](COMO_USAR.txt).

Para atender outro cliente, copie a pasta inteira e edite apenas o
`config.ini` da cópia.

### Rodando com Python

```bash
python baixar_nfse.py
```

Funciona igual ao `.exe`: lê o `config.ini` que estiver ao lado do script.
Não há dependências para instalar (veja [Requisitos](#requisitos)).

## Como funciona

1. Lê o `config.ini` e valida tudo de uma vez (CNPJ com 14 dígitos, datas no
   formato correto, pasta de destino acessível), mostrando todos os problemas
   encontrados em português, sem stack trace.
2. **Localiza o certificado automaticamente:** procura em
   `Cert:\CurrentUser\My` um certificado com chave privada cujo `Subject`
   contenha o CNPJ informado. Trata os casos de nenhum certificado, certificado
   vencido e mais de um certificado válido (nesse caso, lista os candidatos e
   pede o `thumbprint` em `[CERTIFICADO]`).
3. Pagina a API do ADN por NSU, começando de 0.
4. Para cada documento retornado:
   - ignora eventos, DPS e outros tipos que não são NFS-e;
   - decodifica Base64 e descompacta o GZip do payload;
   - lê `<dhEmi>` para descobrir o mês de emissão e aplicar o filtro de
     período;
   - lê `<prest><CNPJ>` para distinguir notas emitidas (prestação) de notas
     recebidas — o ADN retorna os dois tipos no mesmo lote;
   - salva o XML em `{pasta}/{MM}/{chave_acesso}.xml`.
5. Retry com backoff em caso de HTTP 429 (rate limit) ou 5xx.
6. Idempotente: reexecuções pulam arquivos já salvos (nome = chave de acesso).
7. Imprime um resumo: notas salvas por mês, já existentes, fora do período,
   ignoradas (eventos/notas recebidas) e com erro.

## Decisões técnicas

### Por que `curl.exe` do Windows em vez de `requests`?

A biblioteca `requests` (via OpenSSL) não consegue usar certificados do
repositório do Windows — exige um `.pfx` solto no disco mais a senha.

O `curl.exe` nativo do Windows 10/11 usa o backend **Schannel** (CryptoAPI),
que referencia o certificado pelo thumbprint direto no Cert Store
(`--cert CurrentUser\MY\<thumbprint>`), sem exportar a chave privada em
momento nenhum.

**Trade-off:** perde-se portabilidade para Linux/macOS; ganha-se um modelo de
segurança mais forte, com a chave protegida pelo sistema operacional.

### Por que paginar por NSU e não por data de emissão?

A API do ADN pagina exclusivamente por **NSU** (Número Sequencial Único). A
filtragem por data é feita no cliente, após descompactar o XML e ler
`<dhEmi>`. Isso é limitação da API, não escolha do programa.

### Por que HTTP 404 nem sempre é erro?

A API responde HTTP 404 tanto para "fim da paginação"
(`StatusProcessamento = NENHUM_DOCUMENTO_LOCALIZADO`) quanto para outros
erros. O programa sempre inspeciona o corpo da resposta em vez de confiar só
no código HTTP.

### Por que filtrar `<prest><CNPJ>` == CNPJ do cliente?

A API de distribuição retorna documentos em que o contribuinte figura tanto
como **prestador** (nota emitida por ele) quanto como **tomador** (nota
recebida). Como o objetivo é popular a pasta de prestação, filtra-se pelo CNPJ
do prestador extraído do próprio XML.

### Endpoint

`GET {base}/DFe/{NSU}?cnpjConsulta={cnpj}&lote=true`, onde `{base}` é
`https://adn.nfse.gov.br/contribuintes` (produção) ou
`https://adn.producaorestrita.nfse.gov.br/contribuintes` (homologação). O CNPJ
não entra no caminho da URL: é validado contra o CNPJ do certificado usado na
conexão.

## Requisitos

- Windows 10 ou superior (acesso ao Cert Store via Schannel).
- Certificado digital A1 do cliente instalado em `CurrentUser\My`, no usuário
  do Windows que executa o programa.
- `curl.exe` e `powershell.exe` nativos do Windows (já vêm no sistema).
- Para rodar o `.py` ou gerar o `.exe`: Python 3.11+. O programa em si usa só
  a biblioteca padrão.

## Estrutura de saída

```
{pasta do config.ini}/
    01/
        {chave_acesso_1}.xml
        {chave_acesso_2}.xml
    02/
        {chave_acesso_3}.xml
    ...
```

A subpasta é só o mês (`MM`). Se o período abranger mais de um ano, notas do
mesmo mês de anos diferentes cairão na mesma subpasta (sem sobrescrever,
porque o nome do arquivo é a chave de acesso). Para manter anos separados,
rode um período por ano com uma pasta de destino por ano.

## Gerar o executável

```bat
build.bat
```

Instala o PyInstaller e gera `baixar_nfse.exe` (`--onefile --noupx`) nesta
pasta. Quem prefere não confiar em um binário pronto pode gerar o próprio a
partir do código-fonte.

### Antivírus e falso positivo

Executáveis gerados com PyInstaller são frequentemente marcados como ameaça
por antivírus (heurística sobre binário auto-extraível, sem assinatura
digital, que chama `powershell.exe` e `curl.exe`). O build usa `--noupx` para
reduzir isso, mas o executável **não é assinado digitalmente**. Se o seu
antivírus bloquear:

- adicione uma exceção para a pasta onde o programa fica; e/ou
- envie o arquivo como falso positivo ao fabricante do antivírus; ou
- rode direto pelo `baixar_nfse.py`, que não sofre com isso.

## Segurança

- O certificado fica só no Windows Cert Store — o programa nunca exporta a
  chave privada nem lida com senha.
- `config.ini` (CNPJ, thumbprint e caminhos do cliente) está no `.gitignore`;
  só o `config.ini.example` é versionado.
- `.gitignore` também bloqueia `*.pfx`, `*.p12`, `*.pem`, `*.key` e `*.xml`
  (documentos fiscais reais).

## Aviso

Projeto independente, sem vínculo com a Receita Federal ou com o Portal da
NFS-e Nacional. Use por sua conta e risco, respeitando os termos de uso da
API e a LGPD no tratamento dos documentos baixados.

## Licença

[MIT](LICENSE).
