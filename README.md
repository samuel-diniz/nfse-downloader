# NFS-e ADN — Download de notas via API oficial

Programa para **baixar de forma automática, paginada e sem duplicar** as NFS-e
emitidas por um contribuinte, direto da API oficial do **Ambiente de Dados
Nacional (ADN)** da NFS-e Nacional, para um cliente ou para uma lista de
clientes de uma vez.

A autenticação é por **mTLS com certificado digital A1** lido diretamente do
**Windows Cert Store**: a chave privada nunca sai do repositório protegido do
sistema operacional (não há `.pfx` solto no disco e não há senha em lugar
nenhum).

> Versão **2.0.0** (em preparação). Histórico em [`CHANGELOG.md`](CHANGELOG.md).
> Instruções em linguagem simples para quem opera o programa:
> [`COMO_USAR.txt`](COMO_USAR.txt).

## Recursos

- **XML, PDF ou os dois** de cada nota. O PDF (DANFSe v2.0) é gerado
  localmente a partir do XML, no layout da Nota Técnica 008/2026.
- **Modo em lote:** lista de clientes em CSV, período escolhido na hora, relatório
  por execução, um cliente com problema não derruba os outros.
- **Acesso por procuração** (certificado do contador consultando o CNPJ do cliente).
- **Idempotente:** reexecutar nunca duplica, nem quando a equipe já salvou o XML
  com outro sufixo no nome.
- Mensagens de erro em português, com o que fazer para corrigir.

## Requisitos

- Windows 10 ou superior (acesso ao Cert Store via Schannel).
- Certificado digital A1 instalado em `CurrentUser\My`, no usuário do Windows que
  executa o programa: o do cliente, ou o de quem consulta por procuração.
- `curl.exe` e `powershell.exe` nativos do Windows (já vêm no sistema).
- Para rodar o código-fonte: Python 3.11+. **Baixar só XML não exige nenhuma
  biblioteca externa.** Para gerar PDF: `pip install reportlab`.

## Uso rápido: um cliente

O executável não fica no código-fonte: baixe o `.zip` da versão mais recente na
página de [Releases](https://github.com/samuel-diniz/nfse-downloader/releases),
extraia e siga os passos. Confira o SHA-256 informado nas notas da Release
(`certutil -hashfile <arquivo> SHA256`).

1. Execute `baixar_nfse.exe` (ou `python baixar_nfse.py`) uma vez. Ele cria um
   `config.ini` comentado ao lado dele (o modelo está em
   [`config.ini.example`](config.ini.example)).
2. Preencha o `config.ini`: `[CLIENTE]` (nome e **CNPJ**), `[PERIODO]` (datas
   `AAAA-MM-DD`), `[SAIDA]` (`xml`, `pdf` ou `ambos`) e `[DESTINO]` (pasta).
3. Execute de novo. Ao final, a janela do `.exe` espera ENTER para o resumo poder
   ser lido.

Para atender outro cliente, copie a pasta e edite só o `config.ini` da cópia.

## Modo em lote

```bash
python baixar_lote.py --dados C:\caminho\dos\dados --simular   # confere tudo, sem baixar nada
python baixar_lote.py --dados C:\caminho\dos\dados             # pergunta o período e baixa
```

Também há o atalho `executar_lote.bat` (usa `baixar_lote.exe` se existir, senão o
Python). Na primeira execução o programa cria o `lote.ini` na pasta de dados.

### `clientes.csv`

Abre no Excel (CSV com `;` ou `,`, em UTF-8 ou ANSI). Só `nome`, `cnpj` e `pasta`
são obrigatórios; as colunas opcionais podem ser omitidas ou ficar em branco.
Use o [`clientes.csv.example`](clientes.csv.example) como modelo (copie, renomeie
para `clientes.csv` e troque pelos seus clientes):

```text
ativo;nome;cnpj;pasta;pasta_pdf;thumbprint;formato;obs
sim;Exemplo Comércio;12.345.678/0001-99;Z:\FISCAL\101 - EXEMPLO;;;ambos;certificado próprio
sim;Exemplo Serviços;98.765.432/0001-00;Z:\FISCAL\102 - SERVICOS\Prestação;;A1B2C3D4...;;procuração
nao;Cliente Pausado;11.222.333/0001-44;Z:\FISCAL\103 - PAUSADO;;;;
```

| Coluna | Significado |
|---|---|
| `ativo` | `nao` pula o cliente (em branco = ativo) |
| `pasta` | pasta onde ficam as subpastas de mês (`01`, `02`...), caminho completo |
| `pasta_pdf` | pasta separada para os PDFs (em branco = a mesma de `pasta`) |
| `thumbprint` | em branco = certificado do próprio cliente, achado pelo CNPJ; preenchido = certificado de quem tem a procuração |
| `formato` | `xml`, `pdf` ou `ambos` (em branco = o do `lote.ini`) |

Dica: deixe o CNPJ formatado e a coluna como **Texto** no Excel, para ele não
virar número (o programa recompõe zeros perdidos e recusa CNPJ em notação
científica). Cabeçalhos como `Nº;Cliente;CNPJ;Caminho` também são reconhecidos.

### `lote.ini`

Valores comuns a todos os clientes: período padrão (`mes_anterior` ou `fixo`),
formato padrão, `thumbprint_padrao` (certificado de procuração usado quando o
cliente não tem certificado próprio), caminho de rede para trocar `Z:`, ambiente
e pausas entre clientes.

### Período

Ao executar, o programa pergunta: **ENTER** aceita o padrão (mês anterior, ou o
período fixo do `lote.ini`) ou digite outro, como `01/09/2026 a 30/09/2026` ou
`09/2026`. Sem terminal ou para agendar: `--sem-pergunta`, `--periodo 2026-10` ou
`--inicio 01/10/2026 --fim 31/10/2026`.

### Opções

| Opção | Efeito |
|---|---|
| `--simular` | mostra certificado, pasta e ponto de retomada de cada cliente, sem baixar nem gravar |
| `--cliente TEXTO` | só o cliente cujo nome, CNPJ ou número bate (pode repetir) |
| `--refazer` | ignora o controle de NSU e relê todo o histórico |
| `--dados PASTA` | pasta com `clientes.csv`, `lote.ini`, estado, logs e relatórios (ou a variável `NFSE_DADOS`) |
| `--clientes`, `--ini` | caminhos explícitos dos dois arquivos |

### Unidade de rede (`Z:`)

A letra de unidade só existe na sessão de quem a mapeou. Se `Z:` não existir
(tarefa agendada, outro usuário), o programa troca `Z:\` por `caminho_rede` do
`lote.ini`; o `clientes.csv` continua com as letras. Antes de baixar cada
cliente, ele confere se a pasta existe: se não, marca o erro e segue, **sem criar
pastas novas** por causa de um caminho digitado errado. Se nenhuma pasta estiver
acessível, para logo.

### Retomada por NSU

Reexecutar o mesmo período não precisa reler o histórico inteiro. A cobertura de
cada cliente fica em `estado\<CNPJ>.json`, e o programa só retoma de um NSU se uma
execução anterior já cobriu **todo o período pedido**, com o mesmo formato e as
mesmas pastas. Qualquer outro caso relê desde o início: um simples "último NSU"
perderia notas do mês novo que já tinham sido vistas e ignoradas por estarem fora
do período anterior. O estado só é gravado quando o cliente termina sem erros.

### Saídas

- Tabela no console e `relatorios\lote_AAAA-MM-DD_HHMM.csv` (status, notas novas,
  puladas, erros, NSU, validade do certificado, observação) e `logs\...`.
- Status: `OK`, `SEM NOTAS` (o ADN não devolveu nada: conferir CNPJ, certificado e
  procuração; a API responde igual para "sem notas" e "sem acesso"), `COM ERROS`,
  `INCOMPLETO` (instabilidade; repetido uma vez), `ERRO` e `LINHA INVALIDA`.
- Aviso de certificados que vencem em até 30 dias.
- Código de saída: `0` tudo certo, `1` algum cliente falhou, `2` problema de
  configuração, `3` erro inesperado, `130` interrompido.

## PDF (DANFSe)

A API oficial de PDF da Receita foi suspensa pela Nota Técnica 008/2026 (que
passou a geração do DANFSe para os sistemas emissores e fiscais). Por isso o PDF
é **gerado localmente a partir do XML**, no layout DANFSe v2.0: mesma grade, fontes
(Arial e Microsoft Sans Serif, lidas de `C:\Windows\Fonts`), QR Code da consulta
pública e logotipo oficial. Comparado com o PDF emitido pelo portal para a mesma
nota, as palavras coincidem e as posições diferem em menos de 0,4 mm.

Limites: é uma **representação gráfica**; o documento com validade jurídica é o
XML. Não imprime a marca d'água de nota cancelada/substituída e traduz só os
códigos de situação e tributação mais comuns (os demais saem como o código do
XML). A tabela de municípios do IBGE é estática (`ibge_municipios.py`) e precisa
ser regerada se surgir município novo.

Atenção: se a pasta do cliente já tem PDFs com nomes manuais (como
`N F Nº 148.pdf`), o PDF gerado ficaria **ao lado, duplicado**. Use `pdf` ou
`ambos` só onde isso for desejado.

## Certificado e procuração

Sem `thumbprint`, o programa procura em `Cert:\CurrentUser\My` um certificado
com chave privada cujo `Subject` contenha o CNPJ do cliente, e trata os casos de
nenhum certificado, vencido e mais de um válido (lista os candidatos). Com
`thumbprint` (certificado de quem tem a **procuração**), confere se ele existe, se
tem chave privada e se não está vencido. O `cnpj` continua sendo o do cliente; a
API recebe o CNPJ do cliente em `cnpjConsulta`.

**Ressalvas importantes:**

- O acesso por procuração foi **observado em testes** (consultas com e-CPF, para
  clientes com procuração ativa, devolveram as NFS-e deles), mas **não está
  descrito** no manual da API do ADN, que só menciona certificado de mesma raiz
  de CNPJ. A FAQ oficial diz que a plataforma ainda não permite procuração, o que
  parece valer para emissão e portal web, não para a distribuição por NSU. Pode
  mudar sem aviso.
- **Ainda não foi confirmado que a procuração é o fator determinante.** Falta
  testar um CNPJ sem nenhuma procuração ou vínculo.
- Certificado A1 é pessoal: cada usuário deve usar o próprio, com procuração para
  o próprio CPF/CNPJ, em vez de compartilhar um `.pfx`.

## Como funciona

1. Valida a configuração de uma vez (CNPJ, datas, pastas) e mostra todos os
   problemas em português, sem stack trace.
2. Localiza o certificado (acima) e pagina a API do ADN por NSU.
3. Para cada documento: ignora eventos, DPS e outros tipos que não são NFS-e;
   descompacta o XML (Base64 + GZip); lê `<dhEmi>` para o mês e o filtro de período;
   lê `<prest><CNPJ>` para ficar só com as notas **emitidas** pelo cliente (o ADN
   também devolve as recebidas); salva `{pasta}/{MM}/{chave}.xml` e/ou o PDF.
4. Em falhas de rede ou HTTP 429/5xx, repete até 6 vezes com espera crescente
   (3, 6, 12, 24, 48 s) e mostra a mensagem real do `curl.exe` no log.
5. Imprime um resumo: notas por mês, já existentes, fora do período, ignoradas e
   com erro.

### Decisões técnicas

- **`curl.exe` em vez de `requests`:** o `requests` (OpenSSL) não usa certificados
  do repositório do Windows; o `curl.exe` nativo usa o Schannel e referencia o
  certificado pelo thumbprint (`--cert CurrentUser\MY\<thumbprint>`) sem exportar a
  chave. Troca: perde-se portabilidade para Linux/macOS.
- **Paginação por NSU:** é a única que a API oferece; o filtro de data é feito no
  cliente, lendo `<dhEmi>`.
- **HTTP 404 nem sempre é erro:** a API também o usa para "fim da paginação"
  (`NENHUM_DOCUMENTO_LOCALIZADO`), então o corpo da resposta é sempre inspecionado.
- **Endpoint:** `GET {base}/DFe/{NSU}?cnpjConsulta={cnpj}&lote=true`, com `{base}`
  `https://adn.nfse.gov.br/contribuintes` (produção) ou
  `https://adn.producaorestrita.nfse.gov.br/contribuintes` (homologação).

## Estrutura de saída

```
{pasta}/
    01/
        {chave}.xml
        {chave}.pdf
    02/
        ...
```

A subpasta é só o mês. Se o período abranger mais de um ano, notas do mesmo mês
de anos diferentes caem na mesma subpasta (sem sobrescrever, porque o nome é a
chave de acesso). Para separar por ano, use uma pasta de destino por ano.

## Pasta de dados

`config.ini` (um cliente), `clientes.csv`, `lote.ini`, `estado\`, `logs\` e
`relatorios\` ficam, por padrão, ao lado do programa. Para mantê-los **fora da
pasta do código** (recomendado, porque têm dados de clientes), use `--dados
PASTA` ou defina a variável de ambiente `NFSE_DADOS`; o parâmetro vence a variável.

## Desenvolvimento

```
baixar_nfse.py      núcleo e entrada de um cliente (config.ini)
baixar_lote.py      entrada do modo em lote (reaproveita o núcleo)
danfse.py           gerador do PDF (reportlab)
danfse_logo.py      logomarca oficial embutida (base64)
ibge_municipios.py  tabela de municípios do IBGE
config.ini.example  modelo do config.ini
clientes.csv.example  modelo da lista de clientes do lote
executar_lote.bat   atalho do lote
build.bat           gera os executáveis
tests/              testes automatizados (+ fixtures com XML fictício)
CHANGELOG.md        histórico de versões
COMO_USAR.txt       instruções para quem opera
```

**Testes** (offline: sem rede, sem certificado, sem dados reais):

```bash
python -m unittest discover -s tests
```

Sem a `reportlab`, os testes de PDF são pulados. A checagem do texto dos PDFs usa
a `pdfplumber` quando instalada (opcional).

**Executáveis:**

```bat
build.bat
```

Instala o PyInstaller e a `reportlab` e gera `baixar_nfse.exe` e
`baixar_lote.exe` (`--onefile --noupx`) nesta pasta. Quem prefere não confiar em
um binário pronto pode gerar o próprio.

### Antivírus e falso positivo

Executáveis do PyInstaller costumam ser marcados como ameaça por antivírus
(binário auto-extraível, sem assinatura digital, que chama `powershell.exe` e
`curl.exe`). O build usa `--noupx` para reduzir isso, mas o executável **não é
assinado**. Se o seu antivírus bloquear: adicione uma exceção para a pasta do
programa, envie o arquivo como falso positivo ao fabricante, ou rode pelo código
Python.

## Segurança

- O certificado fica só no Windows Cert Store: o programa nunca exporta a chave
  privada nem lida com senha.
- Dados de clientes (`config.ini`, `clientes.csv`, `lote.ini`, estado, logs e
  relatórios) e documentos reais (`*.xml`, `*.pfx` etc.) estão no `.gitignore`;
  só o `config.ini.example` e os XMLs **fictícios** de `tests/fixtures/` são
  versionados.

## Aviso

Projeto independente, sem vínculo com a Receita Federal ou com o Portal da NFS-e
Nacional. Use por sua conta e risco, respeitando os termos de uso da API e a LGPD
no tratamento dos documentos baixados.

## Licença

[MIT](LICENSE).
