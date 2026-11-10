"""
Geracao local do DANFSe (PDF) a partir do XML da NFS-e Nacional.

Segue a Nota Tecnica 008/2026 (layout "DANFSe v2.0") e reproduz a geometria do
PDF emitido pelo portal nacional. A API oficial de geracao do DANFSe foi
suspensa pela propria NT, que passou a atribuir essa geracao aos sistemas
emissores, ERPs e sistemas fiscais.

O PDF e apenas uma representacao grafica: o documento com validade juridica
continua sendo o XML.

Requer a biblioteca "reportlab" (pip install reportlab). As fontes Arial e
Microsoft Sans Serif (exigidas pela NT) sao lidas de C:\\Windows\\Fonts; se nao
existirem, usa Helvetica e FONTES_ORIGINAIS fica False.

Limitacoes conhecidas: nao imprime a marca d'agua de NFS-e cancelada/substituida
e so traduz os codigos de situacao/tributacao mais comuns (os demais saem como
o codigo do XML).
"""

import base64
import io
import re
import xml.etree.ElementTree as ET
from datetime import datetime

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from danfse_logo import LOGO_PNG_B64
from ibge_municipios import MUNICIPIOS

K = 72 / 2.54  # pontos por cm
W, H = A4
PASSO = 0.68  # altura de cada linha de campos (cm)
COL = (0.42, 5.52, 10.62, 15.72)  # x (cm) das quatro colunas de campos

FB, FN = "Arial-Bold", "MSSansSerif"
ASC = {FB: 0.80, FN: 0.80}  # razao ascendente/corpo, calibrada contra o PDF do portal
FONTES_ORIGINAIS = False
_fontes_registradas = False


def registrar_fontes(pasta=r"C:\Windows\Fonts"):
    global FB, FN, FONTES_ORIGINAIS, _fontes_registradas
    if _fontes_registradas:
        return
    try:
        pdfmetrics.registerFont(TTFont("Arial-Bold", pasta + r"\arialbd.ttf"))
        pdfmetrics.registerFont(TTFont("MSSansSerif", pasta + r"\micross.ttf"))
        FB, FN = "Arial-Bold", "MSSansSerif"
        FONTES_ORIGINAIS = True
    except Exception:
        FB, FN = "Helvetica-Bold", "Helvetica"
        ASC[FB] = ASC[FN] = 0.82
        FONTES_ORIGINAIS = False
    _fontes_registradas = True


# ----------------------------------------------------------------------------
# Leitura do XML
# ----------------------------------------------------------------------------
class Nota:
    def __init__(self, xml_texto):
        self.raiz = ET.fromstring(xml_texto)
        for e in self.raiz.iter():
            e.tag = e.tag.split("}")[-1]
        self.inf = self.raiz.find("infNFSe")
        if self.inf is None or self.inf.find("DPS/infDPS") is None:
            raise ValueError("XML nao e uma NFS-e Nacional (infNFSe/DPS/infDPS ausente)")
        self.dps = self.inf.find("DPS/infDPS")
        self.chave = (self.inf.get("Id") or "").removeprefix("NFS")

    def g(self, caminho, base=None):
        """Texto do elemento (caminho relativo a infNFSe, ou a `base`) ou None."""
        no = (base if base is not None else self.inf).find(caminho)
        if no is None or no.text is None or not no.text.strip():
            return None
        return no.text.strip()

    def d(self, caminho):
        """Idem, relativo a infNFSe/DPS/infDPS."""
        return self.g(caminho, self.dps)


# ----------------------------------------------------------------------------
# Tabelas e formatacao
# ----------------------------------------------------------------------------
UF_POR_CODIGO = {
    "11": "RO", "12": "AC", "13": "AM", "14": "RR", "15": "PA", "16": "AP", "17": "TO",
    "21": "MA", "22": "PI", "23": "CE", "24": "RN", "25": "PB", "26": "PE", "27": "AL",
    "28": "SE", "29": "BA", "31": "MG", "32": "ES", "33": "RJ", "35": "SP", "41": "PR",
    "42": "SC", "43": "RS", "50": "MS", "51": "MT", "52": "GO", "53": "DF",
}
OP_SIMPLES = {
    "1": "Não Optante",
    "2": "Optante - Microempreendedor Individual (MEI)",
    "3": "Optante - Microempresa ou Empresa de Pequeno Porte (ME/EPP)",
}
REG_AP_SN = {
    "1": "Regime de apuração dos tributos federais e municipal pelo Simples Nacional",
    "2": "Regime de apuração dos tributos federais pelo SN e o ISSQN pela NFS-e conforme respectivas legislações municipais do tributo",
    "3": "Regime de apuração dos tributos federais e municipal pela NFS-e conforme respectivas legislações federal e municipal de cada tributo",
}
TRIB_ISSQN = {"1": "Operação Tributável", "2": "Imunidade", "3": "Exportação de Serviço", "4": "Não Incidência"}
RET_ISSQN = {"1": "Não Retido", "2": "Retido pelo Tomador", "3": "Retido pelo Intermediário"}
RET_PISCOFINS = {"0": "0 - PIS/COFINS/CSLL Não Retidos"}
TP_EMIT = {"1": "Prestador", "2": "Tomador", "3": "Intermediário"}
SITUACAO = {"100": "NFS-e Gerada"}


def so_digitos(s):
    return re.sub(r"\D", "", s or "")


def traco(v):
    return v if v not in (None, "") else "-"


def limitar(s, n):
    return s if len(s) <= n else s[: n - 3] + "..."


def fmt_doc(s):
    d = so_digitos(s)
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    return s or "-"


def fmt_fone(s):
    d = so_digitos(s)
    if len(d) == 11:
        return f"({d[:2]}) {d[2:7]}-{d[7:]}"
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    return s or "-"


def fmt_ibge(s):
    d = so_digitos(s)
    return f"{d[:2]}.{d[2:]}" if len(d) == 7 else (s or "-")


def fmt_cep(s):
    d = so_digitos(s)
    return f"{d[:2]}.{d[2:5]}-{d[5:]}" if len(d) == 8 else (s or "-")


def fmt_data(s):
    if not s:
        return "-"
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return s


def fmt_dh(s):
    if not s:
        return "-"
    try:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").strftime("%d/%m/%Y %H:%M:%S")
    except ValueError:
        return s


def fmt_moeda(v, vazio="-"):
    if v in (None, ""):
        return vazio
    n = f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {n}"


def fmt_pct(v):
    return "-" if v in (None, "") else f"{float(v):.2f}".replace(".", ",") + "%"


def fmt_nbs(s):
    d = so_digitos(s)
    return f"{d[0]}.{d[1:5]}.{d[5:7]}.{d[7:]}" if len(d) == 9 else (s or "-")


def fmt_ctribnac(s):
    d = so_digitos(s)
    return f"{d[:2]}.{d[2:4]}.{d[4:]}" if len(d) == 6 else (s or "-")


def mun_uf(cod, nomes_xml):
    """'Municipio / UF' a partir do codigo IBGE (nome do XML tem prioridade sobre a tabela)."""
    if not cod:
        return "-"
    nome = nomes_xml.get(cod) or MUNICIPIOS.get(cod) or cod
    return f"{nome} / {UF_POR_CODIGO.get(cod[:2], '-')}"


# ----------------------------------------------------------------------------
# Desenho
# ----------------------------------------------------------------------------
class Desenho:
    def __init__(self, c):
        self.c = c

    def y(self, top_cm):
        return H - top_cm * K

    def linha(self, y, x0=0.3, x1=20.7, lw=0.5):
        self.c.setLineWidth(lw)
        self.c.setStrokeGray(0)
        self.c.line(x0 * K, self.y(y), x1 * K, self.y(y))

    def retangulo(self, x, top, w, h, fundo=None, lw=0.5):
        self.c.setLineWidth(lw)
        self.c.setStrokeGray(0)
        if fundo is not None:  # celulas sombreadas: so preenchimento, sem borda
            self.c.setFillGray(fundo)
            self.c.rect(x * K, self.y(top + h), w * K, h * K, stroke=0, fill=1)
        else:
            self.c.rect(x * K, self.y(top + h), w * K, h * K, stroke=1, fill=0)
        self.c.setFillGray(0)

    def texto(self, x, top, s, fonte, tam, centro=False, cor=None):
        self.c.setFont(fonte, tam)
        if cor:
            self.c.setFillColorRGB(*cor)
        base = self.y(top) - tam * ASC[fonte]
        if centro:
            self.c.drawCentredString(x * K, base, s)
        else:
            self.c.drawString(x * K, base, s)
        self.c.setFillGray(0)

    def largura(self, s, fonte, tam):
        return pdfmetrics.stringWidth(s, fonte, tam) / K

    def quebrar(self, s, fonte, tam, largura_cm):
        linhas = []
        for paragrafo in s.split("\n"):
            atual = ""
            for palavra in paragrafo.split(" "):
                teste = (atual + " " + palavra).strip()
                if self.largura(teste, fonte, tam) <= largura_cm or not atual:
                    atual = teste
                else:
                    linhas.append(atual)
                    atual = palavra
            linhas.append(atual)
        return linhas

    def campo(self, x, top, rotulo, valor, largura=4.95):
        self.texto(x, top, rotulo, FB, 6)
        v = valor
        while self.largura(v, FN, 7) > largura and len(v) > 4:
            v = v[:-4] + "..."
        self.texto(x, top + 0.25, v, FN, 7)

    def paragrafo(self, x, top, linhas, maximo, tam=7, passo=0.28):
        """Imprime ate `maximo` linhas; se faltar espaco, a ultima termina em reticencias."""
        corte = linhas[:maximo]
        if len(linhas) > maximo and corte:
            corte[-1] = limitar(corte[-1] + "...", len(corte[-1]))
        for i, l in enumerate(corte):
            self.texto(x, top + passo * i, l, FN, tam)
        return len(corte)


def _pagina(nota, c):
    d = Desenho(c)
    n = nota
    chave = n.chave
    homolog = n.d("tpAmb") == "2"

    nomes = {}  # codigo IBGE -> nome, quando o proprio XML informa
    for cod, nm in ((n.d("cLocEmi"), n.g("xLocEmi")), (n.g("cLocIncid"), n.g("xLocIncid")),
                    (n.d("serv/locPrest/cLocPrestacao"), n.g("xLocPrestacao"))):
        if cod and nm:
            nomes[cod] = nm

    d.retangulo(0.18, 0.18, 20.64, 29.35, lw=1.0)  # moldura

    # --- cabecalho ---
    for x, w in ((0.3, 5.1), (5.4, 10.2), (15.6, 5.1)):
        d.retangulo(x, 0.2, w, 1.2, fundo=0.949, lw=1.0)
    c.drawImage(ImageReader(io.BytesIO(base64.b64decode(LOGO_PNG_B64))),
                0.42 * K, d.y(0.364 + 0.808), width=4.08 * K, height=0.808 * K, mask="auto")
    d.texto(10.5, 0.48, "DANFSe v2.0", FB, 9, centro=True)
    d.texto(10.5, 0.85, "Documento Auxiliar da NFS-e", FB, 9, centro=True)
    if homolog:
        d.texto(10.5, 1.14, "NFS-e SEM VALIDADE JURÍDICA", FB, 9, centro=True, cor=(1, 0, 0))
    cod_emi = n.d("cLocEmi") or n.g("emit/enderNac/cMun")
    nome_emi = n.g("xLocEmi") or MUNICIPIOS.get(cod_emi or "")
    uf_emit = n.g("emit/enderNac/UF") or UF_POR_CODIGO.get((cod_emi or "")[:2], "-")
    d.texto(15.72, 0.44, f"Município: {traco(nome_emi)} - {uf_emit}", FN, 8)
    d.texto(15.72, 0.75, f"Ambiente Gerador: {traco(n.g('ambGer'))}", FN, 6)
    d.texto(15.72, 0.99, f"Tipo de Ambiente: {traco(n.d('tpAmb'))}", FN, 6)
    d.linha(1.41)

    # --- identificacao da NFS-e ---
    d.texto(0.42, 1.60, "CHAVE DE ACESSO DA NFS-e", FB, 7)
    d.texto(0.42, 1.88, chave, FN, 7)
    ident = [
        (2.32, [("NÚMERO DA NFS-e", traco(n.g("nNFSe"))),
                ("COMPETÊNCIA DA NFS-e", fmt_data(n.d("dCompet"))),
                ("DATA E HORA DA EMISSÃO DA NFS-e", fmt_dh(n.g("dhProc")))]),
        (3.03, [("NÚMERO DA DPS", traco(n.d("nDPS"))),
                ("SÉRIE DA DPS", traco(n.d("serie"))),
                ("DATA E HORA DA EMISSÃO DA DPS", fmt_dh(n.d("dhEmi")))]),
        (3.75, [("EMITENTE DA NFS-e", TP_EMIT.get(n.d("tpEmit"), traco(n.d("tpEmit")))),
                ("SITUAÇÃO DA NFS-e", SITUACAO.get(n.g("cStat"), traco(n.g("cStat")))),
                ("FINALIDADE", traco(n.d("IBSCBS/finNFSe")))]),
    ]
    d.retangulo(0.3, 3.71, 5.1, 0.71, fundo=0.949)
    for top, trio in ident:
        for x, (rot, val) in zip(COL[:3], trio):
            d.texto(x, top, rot, FB, 7)
            d.texto(x, top + 0.28, val, FN, 7)
    qr = QrCodeWidget(f"https://www.nfse.gov.br/ConsultaPublica/?tpc=1&chave={chave}", barLevel="M", barBorder=0)
    x0, y0, x1, y1 = qr.getBounds()
    lado = 1.5875 * K
    dr = Drawing(lado, lado, transform=[lado / (x1 - x0), 0, 0, lado / (y1 - y0), 0, 0])
    dr.add(qr)
    renderPDF.draw(dr, c, 17.356 * K, d.y(1.579 + 1.5875))
    for i, l in enumerate(("A autenticidade desta NFS-e pode ser verificada",
                           "pela leitura deste código QR ou pela consulta da",
                           "chave de acesso no portal nacional da NFS-e")):
        d.texto(15.72, 3.27 + 0.24 * i, l, FN, 6)
    d.linha(4.43)
    y0 = 4.43

    def titulo(y0, texto):
        d.retangulo(0.3, y0 + 0.01, 5.1, 0.67, fundo=0.949)
        d.texto(0.42, y0 + 0.05, texto, FB, 7)

    def lin(y0, k):
        return y0 + 0.04 + k * PASSO

    # --- prestador ---
    titulo(y0, "PRESTADOR / FORNECEDOR")
    e = "emit/enderNac/"
    prest_nome = n.g("emit/xNome") or n.d("prest/xNome")
    prest_end = ", ".join(x for x in (n.g(e + "xLgr"), n.g(e + "nro"), n.g(e + "xCpl"), n.g(e + "xBairro")) if x)
    doc = n.d("prest/CNPJ") or n.d("prest/CPF") or n.d("prest/NIF")
    t = lin(y0, 0)
    d.campo(COL[1], t, "CNPJ / CPF / NIF", fmt_doc(doc))
    d.campo(COL[2], t, "Indicador Municipal (Inscrição)", traco(n.d("prest/IM")))
    d.campo(COL[3], t, "Telefone", fmt_fone(n.d("prest/fone") or n.g("emit/fone")))
    t = lin(y0, 1)
    d.campo(COL[0], t, "Nome / Nome Empresarial", traco(prest_nome), largura=9.9)
    d.campo(COL[2], t, "Município / Sigla UF", f"{traco(nome_emi)} / {uf_emit}")
    d.campo(COL[3], t, "Código IBGE / CEP", f"{fmt_ibge(n.g(e + 'cMun'))} / {fmt_cep(n.g(e + 'CEP'))}")
    t = lin(y0, 2)
    d.campo(COL[0], t, "Endereço", traco(prest_end), largura=9.9)
    d.campo(COL[2], t, "E-mail", traco(n.d("prest/email") or n.g("emit/email")), largura=9.9)
    t = lin(y0, 3)
    d.campo(COL[0], t, "Simples Nacional na Data de Competência",
            limitar(OP_SIMPLES.get(n.d("prest/regTrib/opSimpNac"), "-"), 40))
    d.campo(COL[1], t, "Regime de Apuração Tributária pelo SN",
            limitar(REG_AP_SN.get(n.d("prest/regTrib/regApTribSN"), "-"), 80), largura=14.9)
    y0 += 4 * PASSO
    d.linha(y0)

    # --- tomador / destinatario / intermediario ---
    def bloco_parte(y0, rotulo, base, vazio):
        no = n.dps.find(base)
        if no is None:
            d.texto(10.5, y0 + 0.04, vazio, FN, 7, centro=True)
            d.linha(y0 + 0.295)
            return y0 + 0.295
        titulo(y0, rotulo)
        end = no.find("end")
        cm = n.g("endNac/cMun", end) if end is not None else None
        doc = n.g("CNPJ", no) or n.g("CPF", no) or n.g("NIF", no)
        endereco = ""
        if end is not None:
            endereco = ", ".join(x for x in (n.g("xLgr", end), n.g("nro", end), n.g("xCpl", end), n.g("xBairro", end)) if x)
        t = lin(y0, 0)
        d.campo(COL[1], t, "CNPJ / CPF / NIF", fmt_doc(doc))
        d.campo(COL[2], t, "Indicador Municipal (Inscrição)", traco(n.g("IM", no)))
        d.campo(COL[3], t, "Telefone", fmt_fone(n.g("fone", no)))
        t = lin(y0, 1)
        d.campo(COL[0], t, "Nome / Nome Empresarial", traco(n.g("xNome", no)), largura=9.9)
        d.campo(COL[2], t, "Município / Sigla UF", mun_uf(cm, nomes))
        d.campo(COL[3], t, "Código IBGE / CEP",
                f"{fmt_ibge(cm)} / {fmt_cep(n.g('endNac/CEP', end) if end is not None else None)}")
        t = lin(y0, 2)
        d.campo(COL[0], t, "Endereço", traco(endereco), largura=9.9)
        d.campo(COL[2], t, "E-mail", traco(n.g("email", no)), largura=9.9)
        fim = y0 + 3 * PASSO
        d.linha(fim)
        return fim

    y0 = bloco_parte(y0, "TOMADOR / ADQUIRENTE", "toma", "TOMADOR/ADQUIRENTE DA OPERAÇÃO NÃO IDENTIFICADO NA NFS-e")
    y0 = bloco_parte(y0, "DESTINATÁRIO DA OPERAÇÃO", "IBSCBS/dest", "DESTINATÁRIO DA OPERAÇÃO NÃO IDENTIFICADO NA NFS-e")
    y0 = bloco_parte(y0, "INTERMEDIÁRIO DA OPERAÇÃO", "interm", "INTERMEDIÁRIO DA OPERAÇÃO NÃO IDENTIFICADO NA NFS-e")

    # --- informacoes complementares (calculadas antes para saber o espaco restante) ---
    partes = []
    if n.d("serv/infoCompl/xInfComp"):
        partes.append("Inf. Cont.: " + n.d("serv/infoCompl/xInfComp"))
    if n.d("subst/chSubstda"):
        partes.append("NFS-e Subst.: " + n.d("subst/chSubstda"))
    if n.d("serv/obra/cObra"):
        partes.append("Cod. Obra: " + n.d("serv/obra/cObra"))
    if n.d("IBSCBS/imovel/inscImobFisc"):
        partes.append("Insc. Imob.: " + n.d("IBSCBS/imovel/inscImobFisc"))
    if n.d("serv/atvEvento/idAtvEvt"):
        partes.append("Cod. Evt.: " + n.d("serv/atvEvento/idAtvEvt"))

    def tot(nome_v, nome_p):
        v = n.d(f"valores/trib/totTrib/{nome_v}")
        p = n.d(f"valores/trib/totTrib/{nome_p}")
        return fmt_moeda(v) if v else (fmt_pct(p) if p else "-")

    totais = ("Totais aproximados dos Tributos cfe. Lei n° 12.741/2012: "
              f"Federais: {tot('vTotTribFed', 'pTotTribFed')}; Estaduais: {tot('vTotTribEst', 'pTotTribEst')}; "
              f"Municipais: {tot('vTotTribMun', 'pTotTribMun')};")
    linhas_info = d.quebrar(" | ".join(partes), FN, 7, 20.1) if partes else []
    linhas_info.append(totais)
    ALTURA_INFO = 0.48 + 0.12 + 0.28 * len(linhas_info[:12])  # reserva para o bloco de informacoes (ate 12 linhas)
    ALTURA_FIXA = 2 * PASSO + 2 * PASSO + 4 * PASSO + 2 * PASSO  # trib. municipal, federal, IBS/CBS e totais

    # --- servico prestado ---
    titulo(y0, "SERVIÇO PRESTADO")
    cloc = n.d("serv/locPrest/cLocPrestacao")
    ctrib = fmt_ctribnac(n.d("serv/cServ/cTribNac")) + " / " + traco(n.d("serv/cServ/cTribMun"))
    if n.d("serv/locPrest/cPaisPrestacao"):
        local = f"- / - / {n.d('serv/locPrest/cPaisPrestacao')}"
    else:
        local = f"{mun_uf(cloc, nomes)} / -"
    t = lin(y0, 0)
    d.campo(COL[1], t, "Código de Tributação Nacional/Municipal", ctrib)
    d.campo(COL[2], t, "Código da NBS", fmt_nbs(n.d("serv/cServ/cNBS")) if n.d("serv/cServ/cNBS") else "-")
    d.campo(COL[3], t, "Local da Prestação / Sigla UF / País", local)
    desc_cod = n.d("serv/cServ/xTribMun") or n.g("xTribNac") or "-"
    d.texto(0.42, y0 + 0.71, limitar(desc_cod, 170), FN, 7)
    d.texto(0.42, y0 + 1.14, "Descrição do Serviço", FB, 6)
    linhas = d.quebrar(traco(n.d("serv/cServ/xDescServ")), FN, 7, 20.1)
    espaco = 27.95 - (y0 + 1.39) - ALTURA_FIXA - ALTURA_INFO
    usadas = d.paragrafo(0.42, y0 + 1.39, linhas, max(1, int(espaco / 0.28)))
    y0 = y0 + 1.39 + 0.28 * usadas + 0.12
    d.linha(y0)

    # --- tributacao municipal ---
    titulo(y0, "TRIBUTAÇÃO MUNICIPAL (ISSQN)")
    cinc = n.g("cLocIncid")
    t = lin(y0, 0)
    d.campo(COL[1], t, "Tipo de Tributação do ISSQN", TRIB_ISSQN.get(n.d("valores/trib/tribMun/tribISSQN"), "-"))
    d.campo(COL[2], t, "Município / Sigla UF / País de Incidência do ISSQN", f"{mun_uf(cinc, nomes)} / -")
    t = lin(y0, 1)
    d.campo(COL[0], t, "BC ISSQN", fmt_moeda(n.g("valores/vBC")))
    d.campo(COL[1], t, "Alíquota Aplicada", fmt_pct(n.g("valores/pAliqAplic")))
    d.campo(COL[2], t, "Retenção do ISSQN", RET_ISSQN.get(n.d("valores/trib/tribMun/tpRetISSQN"), "-"))
    d.campo(COL[3], t, "ISSQN Apurado", fmt_moeda(n.g("valores/vISSQN")))
    y0 += 2 * PASSO
    d.linha(y0)

    # --- tributacao federal ---
    d.retangulo(0.3, y0 + 0.01, 5.1, 0.67, fundo=0.949)
    d.texto(0.42, y0 + 0.05, "TRIBUTAÇÃO FEDERAL (EXCETO CBS)", FB, 7)
    pc_no = n.dps.find("valores/trib/tribFed/piscofins")
    tp_ret = n.g("tpRetPisCofins", pc_no) if pc_no is not None else None
    t = lin(y0, 0)
    d.campo(COL[1], t, "IRRF", fmt_moeda(n.d("valores/trib/tribFed/vRetIRRF")))
    d.campo(COL[2], t, "Contribuição Previdenciária - Retida", fmt_moeda(n.d("valores/trib/tribFed/vRetCP")))
    d.campo(COL[3], t, "Contribuições Sociais - Retidas", fmt_moeda(n.d("valores/trib/tribFed/vRetCSLL")))
    t = lin(y0, 1)
    d.campo(COL[0], t, "PIS - Débito Apuração Própria", fmt_moeda(n.g("vPis", pc_no) if pc_no is not None else None))
    d.campo(COL[1], t, "COFINS - Débito Apuração Própria", fmt_moeda(n.g("vCofins", pc_no) if pc_no is not None else None))
    d.campo(COL[2], t, "Descrição Contrib. Sociais - Retidas", RET_PISCOFINS.get(tp_ret, traco(tp_ret)), largura=9.9)
    y0 += 2 * PASSO
    d.linha(y0)

    # --- IBS / CBS ---
    ibs = n.inf.find("IBSCBS")

    def ib(caminho):
        return n.g(caminho, ibs) if ibs is not None else None

    d.retangulo(0.3, y0 + 0.01, 5.1, 0.67, fundo=0.949)
    d.texto(0.42, y0 + 0.05, "TRIBUTAÇÃO IBS/CBS", FB, 7)
    t = lin(y0, 0)
    d.campo(COL[1], t, "CST / cClassTrib",
            f"{traco(n.d('IBSCBS/valores/trib/gIBSCBS/CST'))} / {traco(n.d('IBSCBS/valores/trib/gIBSCBS/cClassTrib'))}")
    d.campo(COL[2], t, "Indicador de Operação / Código IBGE Incidência / Município Incidência / Sigla UF",
            "- / - / - / -", largura=9.9)
    t = lin(y0, 1)
    d.campo(COL[0], t, "Exclusões e Reduções da Base de Cálculo", "R$ 0,00")
    d.campo(COL[1], t, "Base de Cálculo Após Exclusões e Reduções", fmt_moeda(ib("valores/vBC")))
    d.campo(COL[2], t, "Red. Alíquota IBS / Red. Alíquota CBS", "- / - / -")
    d.campo(COL[3], t, "Alíquota - IBS UF / IBS Mun", "- / -")
    t = lin(y0, 2)
    d.campo(COL[0], t, "Alíq. Efetiva Municipal - IBS", fmt_pct(ib("valores/mun/pAliqEfetMun")))
    d.campo(COL[1], t, "Valor Apurado Municipal - IBS", fmt_moeda(ib("totCIBS/gIBS/gIBSMunTot/vIBSMun")))
    d.campo(COL[2], t, "Alíq. Efetiva Estadual - IBS", fmt_pct(ib("valores/uf/pAliqEfetUF")))
    d.campo(COL[3], t, "Valor Apurado Estadual - IBS", fmt_moeda(ib("totCIBS/gIBS/gIBSUFTot/vIBSUF")))
    t = lin(y0, 3)
    d.campo(COL[0], t, "Valor Total Apurado - IBS", fmt_moeda(ib("totCIBS/gIBS/vIBSTot")))
    d.campo(COL[1], t, "Alíquota - CBS", fmt_pct(ib("valores/fed/pCBS")))
    d.campo(COL[2], t, "Alíquota Efetiva - CBS", fmt_pct(ib("valores/fed/pAliqEfetCBS")))
    d.campo(COL[3], t, "Valor Total Apurado - CBS", fmt_moeda(ib("totCIBS/gCBS/vCBS")))
    y0 += 4 * PASSO
    d.linha(y0)

    # --- valor total ---
    d.retangulo(0.3, y0 + 0.01, 5.1, 0.67, fundo=0.949)
    d.retangulo(15.6, y0 + 0.68, 5.1, 0.67, fundo=0.949)
    d.texto(0.42, y0 + 0.04, "VALOR TOTAL DA NFS-e", FB, 7)
    t = lin(y0, 0)
    d.campo(COL[1], t, "VALOR DA OPERAÇÃO / SERVIÇO", fmt_moeda(n.d("valores/vServPrest/vServ")))
    d.campo(COL[2], t, "Desconto Incondicionado", fmt_moeda(n.d("valores/vDescCondIncond/vDescIncond")))
    d.campo(COL[3], t, "Desconto Condicionado", fmt_moeda(n.d("valores/vDescCondIncond/vDescCond")))
    t = lin(y0, 1)
    d.campo(COL[0], t, "Total das Retenções (ISSQN / Federais)", fmt_moeda(n.g("valores/vTotalRet")))
    d.campo(COL[1], t, "VALOR LÍQUIDO DA NFS-e", fmt_moeda(n.g("valores/vLiq")))
    d.campo(COL[2], t, "Total do IBS/CBS", "R$ 0,00")
    d.campo(COL[3], t, "VALOR LÍQUIDO DA NFS-e + IBS/CBS", fmt_moeda(ib("totCIBS/vTotNF"), vazio="R$ 0,00"))
    y0 += 2 * PASSO
    d.linha(y0)

    # --- informacoes complementares ---
    d.texto(0.42, y0 + 0.05, "INFORMAÇÕES COMPLEMENTARES", FB, 7)
    maximo = max(1, int((27.95 - (y0 + 0.48)) / 0.28))
    if len(linhas_info) > maximo:  # nunca descartar a linha fixa de "Totais aproximados"
        linhas_info = linhas_info[: maximo - 1] + [linhas_info[-1]]
        linhas_info[-2] = limitar(linhas_info[-2] + "...", len(linhas_info[-2]))
    d.paragrafo(0.42, y0 + 0.48, linhas_info, maximo)

    # --- canhoto ---
    for xa, xb in ((0.3, 5.44), (5.4, 10.54), (10.5, 20.74)):
        d.linha(28.07, xa, xb, lw=1.0)
        d.linha(28.78, xa, xb, lw=1.0)
    c.setLineWidth(1.0)
    for xv in (0.32, 5.42, 10.52, 20.72):
        c.line(xv * K, d.y(28.09), xv * K, d.y(28.8))
    d.texto(0.46, 28.12, "DATA CIENTIFICAÇÃO:", FB, 6)
    d.texto(5.56, 28.12, "IDENTIFICAÇÃO E ASSINATURA", FB, 6)
    d.texto(10.66, 28.12, "N° NFS-e / CHAVE NFS-e", FB, 6)
    d.texto(10.66, 28.37, f"{traco(n.g('nNFSe'))} / {chave}", FN, 7)


def gerar_pdf(xml_texto, destino):
    """Gera o DANFSe da NFS-e `xml_texto` em `destino` (caminho de arquivo). Retorna a chave de acesso."""
    registrar_fontes()
    nota = Nota(xml_texto)
    c = canvas.Canvas(destino, pagesize=A4)
    c.setTitle(f"DANFSe {nota.chave}")
    _pagina(nota, c)
    c.showPage()
    c.save()
    return nota.chave
