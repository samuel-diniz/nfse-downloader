# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).
Versões seguem [SemVer](https://semver.org/lang/pt-BR/).

## [2.0.0] — em preparação

### Adicionado
- **Modo em lote** (`baixar_lote.py` e o atalho `executar_lote.bat`): lê uma lista
  de clientes (`clientes.csv`) e baixa as notas de todos, um cliente após o outro.
  Um cliente com problema não interrompe os demais; os que falham por
  instabilidade do ADN são repetidos uma vez. Gera tabela no console, relatório
  CSV e log por execução, e devolve um código de saída para o agendador de tarefas.
- **Período escolhido na hora de executar** (ENTER aceita o mês anterior;
  aceita `01/09/2026 a 30/09/2026` ou `09/2026`), também por `--periodo`,
  `--inicio` e `--fim`.
- **Retomada por NSU** no lote: só retoma de onde parou quando uma execução
  anterior já cobriu todo o período pedido, com o mesmo formato e as mesmas
  pastas. Caso contrário relê o histórico (`--refazer` força a releitura).
- **Letra de unidade de rede**: `Z:\` é trocada pelo caminho de rede
  (`[REDE]` do `lote.ini`) quando a letra não existe, como em tarefas agendadas.
- **PDF (DANFSe) gerado localmente** a partir do XML, no layout da Nota
  Técnica 008/2026 (DANFSe v2.0), com QR Code e logotipo oficial. Opção
  `[SAIDA] formato = xml | pdf | ambos` e `pasta_pdf` opcional. A API de PDF da
  Receita foi suspensa por essa mesma Nota Técnica.
- **Tabela de municípios do IBGE** embutida (nomes e UF do tomador e do local de
  incidência/prestação).
- **Acesso por procuração**: o `thumbprint` informado manualmente é validado
  (existe, tem chave privada, não está vencido), e o lote aceita um
  `thumbprint_padrao` como reserva para clientes sem certificado próprio.
- **Pasta de dados separada do código** (`--dados` ou variável `NFSE_DADOS`),
  para manter `clientes.csv`, `lote.ini`, estado, logs e relatórios fora do
  repositório. `--versao` mostra a versão.
- Testes automatizados (`tests/`), `.gitattributes` e este changelog.

### Alterado
- **Mais paciência com o ADN:** até 6 tentativas, com esperas que dobram
  (3, 6, 12, 24, 48 s) em falhas de rede, e a mensagem real do `curl.exe`
  passa a aparecer no log (antes só aparecia "codigo 56").
- **"Já existe" mais esperto:** uma nota é considerada salva também quando o
  arquivo tem sufixo depois da chave (ex.: `<chave> - 02.01.xml`), evitando
  duplicar o que a equipe já guardou.
- No lote, a pasta base do cliente **não é criada**: um caminho digitado errado
  vira erro no relatório, em vez de criar pastas novas no servidor.
- Mensagens de erro de certificado orientam o passo a passo da procuração.
- `executar_download` devolve um resultado estruturado (`ResultadoDownload`).
- `build.bat` gera os dois executáveis (`baixar_nfse.exe` e `baixar_lote.exe`).

### Observações
- O acesso por procuração foi observado em testes, mas não está descrito na
  documentação oficial do ADN. Ainda não foi testado o que a API responde para
  um CNPJ sem procuração.
- O PDF é uma representação gráfica gerada pelo programa; o documento com
  validade jurídica continua sendo o XML. Não imprime a marca d'água de nota
  cancelada ou substituída e traduz só os códigos de situação/tributação mais
  comuns (os demais saem como o código do XML).

## [1.0.0]

### Adicionado
- Download das NFS-e emitidas por um cliente, via API do ADN, com autenticação
  mTLS pelo certificado A1 do Windows Cert Store (sem `.pfx` solto nem senha).
- Configuração por `config.ini` (criado e comentado na primeira execução) e
  detecção automática do certificado pelo CNPJ.
- Filtro de notas emitidas (prestador) e de tipo de documento; XMLs salvos por
  mês, sem duplicar.
- Empacotamento em `.exe` (PyInstaller), mensagens de erro em português e licença MIT.
