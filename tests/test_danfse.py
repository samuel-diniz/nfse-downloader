"""Testes do gerador de DANFSe (danfse.py). Exigem a biblioteca reportlab; sem ela, sao pulados.

A checagem do texto do PDF usa a pdfplumber quando estiver instalada (opcional).
"""
import shutil
import sys
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import danfse
except ImportError as exc:  # reportlab ausente
    raise unittest.SkipTest(f"reportlab nao instalada ({exc})")

_CRIADAS = []


def pasta_temp() -> Path:
    """Pasta temporaria, apagada ao fim do modulo de testes."""
    pasta = Path(tempfile.mkdtemp())
    _CRIADAS.append(pasta)
    return pasta


def _limpar_temporarias():
    for pasta in _CRIADAS:
        shutil.rmtree(pasta, ignore_errors=True)


def tearDownModule():
    _limpar_temporarias()


FIXTURE = Path(__file__).parent / "fixtures" / "nfse_exemplo.xml"
XML = FIXTURE.read_text(encoding="utf-8")
CHAVE = "35039012212345678000199000000000000126101234567890"


class TestFormatacao(unittest.TestCase):
    def test_documentos(self):
        self.assertEqual(danfse.fmt_doc("12345678000199"), "12.345.678/0001-99")
        self.assertEqual(danfse.fmt_doc("12345678909"), "123.456.789-09")
        self.assertEqual(danfse.fmt_doc(None), "-")

    def test_telefone(self):
        self.assertEqual(danfse.fmt_fone("11940000000"), "(11) 94000-0000")
        self.assertEqual(danfse.fmt_fone("1140000000"), "(11) 4000-0000")
        self.assertEqual(danfse.fmt_fone(""), "-")

    def test_ibge_e_cep(self):
        self.assertEqual(danfse.fmt_ibge("3503901"), "35.03901")
        self.assertEqual(danfse.fmt_cep("07438550"), "07.438-550")
        self.assertEqual(danfse.fmt_cep("7434325"), "7434325")  # o portal nao formata CEP de 7 digitos

    def test_valores(self):
        self.assertEqual(danfse.fmt_moeda("1234.5"), "R$ 1.234,50")
        self.assertEqual(danfse.fmt_moeda(None), "-")
        self.assertEqual(danfse.fmt_moeda(None, vazio="R$ 0,00"), "R$ 0,00")
        self.assertEqual(danfse.fmt_pct("3.5"), "3,50%")

    def test_datas(self):
        self.assertEqual(danfse.fmt_data("2026-10-31"), "31/10/2026")
        self.assertEqual(danfse.fmt_dh("2026-10-31T15:40:10-03:00"), "31/10/2026 15:40:10")

    def test_codigos(self):
        self.assertEqual(danfse.fmt_ctribnac("050801"), "05.08.01")
        self.assertEqual(danfse.fmt_nbs("123456789"), "1.2345.67.89")

    def test_limitar_com_reticencias(self):
        self.assertEqual(danfse.limitar("Optante - Microempresa ou Empresa de Pequeno Porte", 40), "Optante - Microempresa ou Empresa de ...")
        self.assertEqual(danfse.limitar("curto", 40), "curto")


class TestMunicipios(unittest.TestCase):
    def test_tabela_do_ibge(self):
        self.assertGreater(len(danfse.MUNICIPIOS), 5500)
        self.assertEqual(danfse.MUNICIPIOS["3550308"], "São Paulo")

    def test_nome_e_uf_pelo_codigo(self):
        self.assertEqual(danfse.mun_uf("3550308", {}), "São Paulo / SP")
        self.assertEqual(danfse.mun_uf("4305355", {}).split(" / ")[1], "RS")

    def test_nome_do_xml_tem_prioridade(self):
        self.assertEqual(danfse.mun_uf("3550308", {"3550308": "Nome do XML"}), "Nome do XML / SP")

    def test_codigo_desconhecido_e_ausente(self):
        self.assertEqual(danfse.mun_uf("3599999", {}), "3599999 / SP")
        self.assertEqual(danfse.mun_uf(None, {}), "-")


class TestNota(unittest.TestCase):
    def test_leitura(self):
        n = danfse.Nota(XML)
        self.assertEqual(n.chave, CHAVE)
        self.assertEqual(n.g("nNFSe"), "1")
        self.assertEqual(n.d("serie"), "1")
        self.assertIsNone(n.d("campo/que/nao/existe"))

    def test_xml_que_nao_e_nfse(self):
        with self.assertRaises(ValueError):
            danfse.Nota("<outra><coisa/></outra>")


class TestPdf(unittest.TestCase):
    def gerar(self, xml=XML):
        destino = pasta_temp() / "nota.pdf"
        self.assertEqual(danfse.gerar_pdf(xml, str(destino)), CHAVE)
        return destino

    def test_gera_pdf_valido(self):
        dados = self.gerar().read_bytes()
        self.assertTrue(dados.startswith(b"%PDF"))
        self.assertTrue(dados.rstrip().endswith(b"%%EOF"))
        self.assertGreater(len(dados), 20_000)  # logo + fontes embutidas

    def test_textos_muito_longos_nao_quebram(self):
        longo = XML.replace("Serviço de teste", "Serviço " + "descrição muito longa " * 400) \
                   .replace("Fonte: IBPT.", "Fonte: IBPT. " + "informação adicional " * 300)
        self.assertTrue(self.gerar(longo).read_bytes().startswith(b"%PDF"))

    def test_nota_sem_tomador(self):
        i, f = XML.index("<toma>"), XML.index("</toma>") + len("</toma>")
        self.assertTrue(self.gerar(XML[:i] + XML[f:]).read_bytes().startswith(b"%PDF"))

    @unittest.skipUnless(find_spec("pdfplumber"), "pdfplumber nao instalada (checagem do texto e opcional)")
    def test_conteudo_do_pdf(self):
        import pdfplumber
        with pdfplumber.open(self.gerar()) as pdf:
            self.assertEqual(len(pdf.pages), 1)
            texto = pdf.pages[0].extract_text().replace(" ", "")
        for esperado in ("DANFSev2.0", CHAVE, "SãoPaulo/SP", "R$100,00", "05.08.01", "NFS-eGerada", "Prestador"):
            self.assertIn(esperado, texto)
        self.assertNotIn("SEMVALIDADE", texto)

    @unittest.skipUnless(find_spec("pdfplumber"), "pdfplumber nao instalada (checagem do texto e opcional)")
    def test_homologacao_leva_a_marca(self):
        import pdfplumber
        with pdfplumber.open(self.gerar(XML.replace("<tpAmb>1</tpAmb>", "<tpAmb>2</tpAmb>"))) as pdf:
            self.assertIn("SEMVALIDADEJURÍDICA", pdf.pages[0].extract_text().replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
