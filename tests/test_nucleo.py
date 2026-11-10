"""Testes do nucleo (baixar_nfse.py): XML, arquivos, retentativas, configuracao e o loop de download.

Rodam offline: nada de rede, certificado ou dados reais (a API do ADN e simulada).
"""
import base64
import gzip
import json
import logging
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import baixar_nfse as nf  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "nfse_exemplo.xml"
XML = FIXTURE.read_text(encoding="utf-8")
CHAVE = "35039012212345678000199000000000000126101234567890"
CNPJ = "12345678000199"


_CRIADAS = []


def pasta_temp() -> Path:
    """Pasta temporaria, apagada ao fim do modulo de testes."""
    pasta = Path(tempfile.mkdtemp())
    _CRIADAS.append(pasta)
    return pasta


def _limpar_temporarias():
    for pasta in _CRIADAS:
        shutil.rmtree(pasta, ignore_errors=True)


def setUpModule():
    logging.disable(logging.CRITICAL)  # o log do programa so atrapalha a saida dos testes


def tearDownModule():
    logging.disable(logging.NOTSET)
    _limpar_temporarias()


def empacotar(xml_texto: str) -> str:
    return base64.b64encode(gzip.compress(xml_texto.encode("utf-8"))).decode()


def documento(nsu, chave, xml_texto, tipo="NFSE"):
    return {"NSU": nsu, "ChaveAcesso": chave, "TipoDocumento": tipo, "ArquivoXml": empacotar(xml_texto)}


class TestXml(unittest.TestCase):
    def test_descompactar_xml(self):
        self.assertEqual(nf.descompactar_xml(empacotar(XML)), XML)

    def test_data_de_emissao(self):
        self.assertEqual(nf.extrair_data_emissao(XML), datetime(2026, 10, 15))

    def test_cnpj_do_prestador(self):
        self.assertEqual(nf.extrair_cnpj_prestador(XML), CNPJ)

    def test_cnpj_do_prestador_ausente(self):
        with self.assertRaises(ValueError):
            nf.extrair_cnpj_prestador("<NFSe><infNFSe/></NFSe>")

    def test_periodo_inclusivo(self):
        ini, fim = datetime(2026, 10, 1), datetime(2026, 10, 31)
        self.assertTrue(nf.dentro_do_periodo(datetime(2026, 10, 1), ini, fim))
        self.assertTrue(nf.dentro_do_periodo(datetime(2026, 10, 31), ini, fim))
        self.assertFalse(nf.dentro_do_periodo(datetime(2026, 11, 1), ini, fim))


class TestArquivos(unittest.TestCase):
    def setUp(self):
        nf._INDICE_PASTAS.clear()
        self.tmp = pasta_temp()
        self.dt = datetime(2026, 10, 15)

    def test_salva_e_nao_duplica(self):
        self.assertTrue(nf.salvar_xml(XML, CHAVE, self.dt, self.tmp))
        self.assertTrue((self.tmp / "10" / f"{CHAVE}.xml").exists())
        self.assertFalse(nf.salvar_xml(XML, CHAVE, self.dt, self.tmp))

    def test_reconhece_nome_com_sufixo(self):
        (self.tmp / "10").mkdir()
        (self.tmp / "10" / f"{CHAVE} - 02.01.xml").write_text("x", encoding="utf-8")
        self.assertFalse(nf.salvar_xml(XML, CHAVE, self.dt, self.tmp))
        self.assertEqual(len(list((self.tmp / "10").glob("*.xml"))), 1)

    def test_pdf_nao_conta_como_xml(self):
        (self.tmp / "10").mkdir()
        (self.tmp / "10" / f"{CHAVE}.pdf").write_text("x", encoding="utf-8")
        self.assertTrue(nf.salvar_xml(XML, CHAVE, self.dt, self.tmp))

    def test_pdf_com_nome_manual_nao_conta(self):
        (self.tmp / "10").mkdir()
        (self.tmp / "10" / "N F Nº 148.pdf").write_text("x", encoding="utf-8")
        self.assertFalse(nf.ja_existe(self.tmp / "10", CHAVE, ".pdf"))

    def test_mes_com_dois_digitos(self):
        nf.salvar_xml(XML, CHAVE, datetime(2026, 3, 2), self.tmp)
        self.assertTrue((self.tmp / "03" / f"{CHAVE}.xml").exists())


class TestRetentativas(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(nf.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_recupera_apos_http_429(self):
        with mock.patch.object(nf, "chamar_api", side_effect=[(429, ""), (200, "ok")]) as api:
            self.assertEqual(nf.chamar_com_retry("u", "T"), (200, "ok"))
        self.assertEqual(api.call_count, 2)

    def test_recupera_apos_falhas_de_rede(self):
        erro = RuntimeError("curl.exe falhou (codigo 56)")
        with mock.patch.object(nf, "chamar_api", side_effect=[erro, erro, (200, "ok")]) as api:
            self.assertEqual(nf.chamar_com_retry("u", "T"), (200, "ok"))
        self.assertEqual(api.call_count, 3)
        esperas = [c.args[0] for c in self.sleep.call_args_list]
        self.assertEqual(esperas, [nf.BACKOFF_REDE_SEGUNDOS, nf.BACKOFF_REDE_SEGUNDOS * 2])

    def test_desiste_depois_do_maximo(self):
        with mock.patch.object(nf, "chamar_api", side_effect=RuntimeError("falha")) as api:
            with self.assertRaises(RuntimeError):
                nf.chamar_com_retry("u", "T")
        self.assertEqual(api.call_count, nf.MAX_TENTATIVAS)

    def test_404_nao_e_retentado(self):
        with mock.patch.object(nf, "chamar_api", return_value=(404, "{}")) as api:
            self.assertEqual(nf.chamar_com_retry("u", "T"), (404, "{}"))
        self.assertEqual(api.call_count, 1)


class TestConfiguracao(unittest.TestCase):
    def setUp(self):
        self.tmp = pasta_temp()
        nf.definir_pasta_dados(self.tmp)
        self.addCleanup(nf.definir_pasta_dados, None)

    def escrever(self, **campos):
        base = {"cnpj": CNPJ, "ini": "2026-10-01", "fim": "2026-10-31", "formato": "xml", "pasta": str(self.tmp / "saida")}
        base.update(campos)
        (self.tmp / "config.ini").write_text(
            f"[CLIENTE]\nnome = Teste\ncnpj = {base['cnpj']}\n[PERIODO]\ndata_inicio = {base['ini']}\n"
            f"data_fim = {base['fim']}\n[SAIDA]\nformato = {base['formato']}\n[DESTINO]\npasta = {base['pasta']}\n",
            encoding="utf-8")

    def test_config_valida(self):
        self.escrever()
        c = nf.carregar_configuracao()
        self.assertEqual((c.cnpj, c.formato, c.pasta_pdf), (CNPJ, "xml", c.pasta_destino))

    def test_cnpj_com_pontuacao(self):
        self.escrever(cnpj="12.345.678/0001-99")
        self.assertEqual(nf.carregar_configuracao().cnpj, CNPJ)

    def test_erros_sao_agrupados(self):
        self.escrever(cnpj="123", ini="31/10/2026", formato="docx")
        with self.assertRaises(nf.ConfigError) as ctx:
            nf.carregar_configuracao()
        texto = "\n".join(ctx.exception.mensagens)
        for esperado in ("cnpj", "data_inicio", "formato"):
            self.assertIn(esperado, texto)

    def test_periodo_invertido(self):
        self.escrever(ini="2026-11-01", fim="2026-10-01")
        with self.assertRaises(nf.ConfigError):
            nf.carregar_configuracao()

    def test_pasta_de_dados_pelo_ambiente(self):
        nf.definir_pasta_dados(None)
        outra = pasta_temp()
        with mock.patch.dict("os.environ", {"NFSE_DADOS": str(outra)}):
            self.assertEqual(nf.pasta_base(), outra.resolve())
            nf.definir_pasta_dados(self.tmp)  # o parametro explicito vence a variavel
            self.assertEqual(nf.pasta_base(), self.tmp.resolve())

    def test_pasta_de_dados_inexistente(self):
        nf.definir_pasta_dados(self.tmp / "nao_existe")
        with self.assertRaises(nf.ConfigError):
            nf.validar_pasta_dados()


class TestDownload(unittest.TestCase):
    """Loop de download com o ADN simulado (uma pagina com tres tipos de documento)."""

    def setUp(self):
        nf._INDICE_PASTAS.clear()
        self.tmp = pasta_temp()
        for patcher in (mock.patch.object(nf, "CURL_EXE", sys.executable), mock.patch.object(nf.time, "sleep")):
            patcher.start()
            self.addCleanup(patcher.stop)
        recebida = XML.replace("<CNPJ>12345678000199</CNPJ>", "<CNPJ>99999999000191</CNPJ>")
        self.pagina = {"StatusProcessamento": "DOCUMENTOS_LOCALIZADOS", "LoteDFe": [
            documento(1, CHAVE, XML),                                   # emitida pelo cliente
            documento(2, "9" * 50, recebida),                            # recebida (outro prestador)
            documento(3, "8" * 50, XML, tipo="EVENTO"),                  # evento: nao e NFS-e
        ]}

    def config(self, formato="xml", ini=datetime(2026, 10, 1), fim=datetime(2026, 10, 31)):
        return nf.Configuracao("Teste", CNPJ, "", ini, fim, self.tmp / "xml", self.tmp / "pdf", formato, False)

    def rodar(self, resposta, **kw):
        with mock.patch.object(nf, "chamar_com_retry", return_value=(200, json.dumps(resposta))):
            return nf.executar_download(self.config(**{k: v for k, v in kw.items() if k in ("formato", "ini", "fim")}),
                                        "T", "cert de teste",
                                        **{k: v for k, v in kw.items() if k not in ("formato", "ini", "fim")})

    def test_filtra_e_salva(self):
        r = self.rodar(self.pagina)
        self.assertEqual((r.xml_novos, r.recebidas, r.nao_nfse, r.erros), (1, 1, 1, 0))
        self.assertTrue(r.completo)
        self.assertEqual(r.ultimo_nsu, 3)
        self.assertTrue((self.tmp / "xml" / "10" / f"{CHAVE}.xml").exists())

    def test_segunda_execucao_pula(self):
        self.rodar(self.pagina)
        r = self.rodar(self.pagina)
        self.assertEqual((r.xml_novos, r.xml_pulados), (0, 1))

    def test_fora_do_periodo(self):
        r = self.rodar(self.pagina, ini=datetime(2026, 11, 1), fim=datetime(2026, 11, 30))
        self.assertEqual((r.xml_novos, r.fora_periodo), (0, 1))

    def test_sem_documentos(self):
        r = self.rodar({"StatusProcessamento": "NENHUM_DOCUMENTO_LOCALIZADO", "LoteDFe": []})
        self.assertTrue(r.completo)
        self.assertEqual(r.docs_vistos, 0)

    def test_falha_de_rede_deixa_incompleto(self):
        with mock.patch.object(nf, "chamar_com_retry", side_effect=RuntimeError("curl 56")):
            r = nf.executar_download(self.config(), "T", "cert")
        self.assertFalse(r.completo)
        self.assertIn("curl 56", r.erro)

    def test_rejeicao_da_api(self):
        r = self.rodar({"StatusProcessamento": "REJEICAO", "Erros": [{"Codigo": "E1", "Descricao": "negado"}]})
        self.assertFalse(r.completo)
        self.assertIn("negado", r.erro)

    def test_retomada_comeca_do_nsu_informado(self):
        with mock.patch.object(nf, "chamar_com_retry", return_value=(200, json.dumps(self.pagina))) as api:
            nf.executar_download(self.config(), "T", "cert", nsu_inicial=250)
        self.assertIn("/DFe/250?", api.call_args.args[0])

    def test_pasta_base_inexistente_no_lote(self):
        with self.assertRaises(nf.ConfigError):
            self.rodar(self.pagina, criar_pasta_base=False)

    def test_pasta_base_criada_no_modo_simples(self):
        self.rodar(self.pagina, criar_pasta_base=True)
        self.assertTrue((self.tmp / "xml").is_dir())

    def test_gera_pdf_junto(self):
        try:
            import reportlab  # noqa: F401
        except ImportError:
            self.skipTest("reportlab nao instalada")
        r = self.rodar(self.pagina, formato="ambos")
        self.assertEqual((r.xml_novos, r.pdf_novos, r.erros), (1, 1, 0))
        pdf = self.tmp / "pdf" / "10" / f"{CHAVE}.pdf"
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
        self.assertEqual(list((self.tmp / "pdf" / "10").glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
