"""Testes do modo em lote (baixar_lote.py): periodo, CSV, filtro, Z: -> rede, controle de NSU e status por cliente.

Rodam offline: o download e a busca de certificado sao simulados.
"""
import builtins
import io
import logging
import shutil
import sys
import tempfile
import types
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import baixar_lote as bl  # noqa: E402
import baixar_nfse as nf  # noqa: E402
from baixar_nfse import CertificadoNaoEncontrado, ConfigError, ResultadoDownload  # noqa: E402


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


def csv_temporario(texto, encoding="utf-8-sig") -> Path:
    caminho = pasta_temp() / "clientes.csv"
    caminho.write_bytes(texto.encode(encoding))
    return caminho


class TestPeriodo(unittest.TestCase):
    def test_formatos_aceitos(self):
        esperado = (date(2026, 9, 1), date(2026, 9, 30))
        for texto in ("01/09/2026 a 30/09/2026", "01/09/2026 - 30/09/2026", "2026-09-01 2026-09-30", "09/2026", "2026-09"):
            self.assertEqual(bl.interpretar_periodo(texto), esperado, texto)

    def test_mes_inteiro(self):
        self.assertEqual(bl.interpretar_periodo("2026-02"), (date(2026, 2, 1), date(2026, 2, 28)))
        self.assertEqual(bl.interpretar_periodo("12/2026"), (date(2026, 12, 1), date(2026, 12, 31)))

    def test_invalidos(self):
        for texto in ("30/09/2026 a 01/09/2026", "13/2026", "abc", "01/09/2026", ""):
            with self.assertRaises(ValueError, msg=texto):
                bl.interpretar_periodo(texto)

    def test_mes_anterior(self):
        self.assertEqual(bl.mes_anterior(date(2026, 1, 15)), (date(2025, 12, 1), date(2025, 12, 31)))
        self.assertEqual(bl.mes_anterior(date(2026, 3, 1)), (date(2026, 2, 1), date(2026, 2, 28)))

    def args(self, **kw):
        base = dict(periodo=None, inicio=None, fim=None, sem_pergunta=False, simular=False)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def resolver(self, args, respostas=(), tty=True):
        padroes = types.SimpleNamespace(periodo_tipo="mes_anterior", data_inicio=None, data_fim=None)
        entradas = iter(respostas)
        with mock.patch.object(sys, "stdin", types.SimpleNamespace(isatty=lambda: tty)), \
                mock.patch.object(builtins, "input", lambda *_: next(entradas)), \
                mock.patch.object(builtins, "print"):
            return bl.resolver_periodo(args, padroes)

    def test_enter_aceita_o_padrao(self):
        self.assertEqual(self.resolver(self.args(), [""]), bl.mes_anterior())

    def test_resposta_invalida_pergunta_de_novo(self):
        self.assertEqual(self.resolver(self.args(), ["lixo", "01/09/2026 a 15/09/2026"]),
                         (date(2026, 9, 1), date(2026, 9, 15)))

    def test_sem_pergunta_e_sem_terminal(self):
        self.assertEqual(self.resolver(self.args(sem_pergunta=True)), bl.mes_anterior())
        self.assertEqual(self.resolver(self.args(), tty=False), bl.mes_anterior())

    def test_argumento_vence_o_prompt(self):
        self.assertEqual(self.resolver(self.args(periodo="2026-05")), (date(2026, 5, 1), date(2026, 5, 31)))

    def test_inicio_sem_fim(self):
        with self.assertRaises(ConfigError):
            self.resolver(self.args(inicio="01/05/2026"))


class TestPastas(unittest.TestCase):
    REDE = r"\\SERVIDOR\Pasta"

    def traduzir(self, caminho, existe, rede=REDE):
        return bl.traduzir_unidade(caminho, "Z:", rede, existe=lambda p: existe)

    def test_unidade_ausente_vira_caminho_de_rede(self):
        self.assertEqual(self.traduzir(r"Z:\FISCAL\A", False), r"\\SERVIDOR\Pasta\FISCAL\A")

    def test_unidade_presente_nao_muda(self):
        self.assertEqual(self.traduzir(r"Z:\FISCAL\A", True), r"Z:\FISCAL\A")

    def test_letra_minuscula(self):
        self.assertEqual(self.traduzir(r"z:\FISCAL", False), r"\\SERVIDOR\Pasta\FISCAL")

    def test_outros_caminhos_nao_mudam(self):
        for caminho in (r"C:\x", r"\\SRV\x"):
            self.assertEqual(self.traduzir(caminho, False), caminho)

    def test_sem_caminho_de_rede_configurado(self):
        self.assertEqual(self.traduzir(r"Z:\x", False, rede=""), r"Z:\x")

    def test_barra_final_no_caminho_de_rede(self):
        self.assertEqual(self.traduzir(r"Z:\x", False, rede=self.REDE + "\\"), r"\\SERVIDOR\Pasta\x")


class TestClientesCsv(unittest.TestCase):
    def test_cabecalho_da_planilha_original(self):
        # exportado do Excel em portugues: ';', ANSI e colunas "Nº;Cliente;CNPJ;Caminho"
        clientes, problemas = bl.ler_clientes(csv_temporario(
            "Nº;Cliente;CNPJ;Caminho\n7;Fulano Ltda;12.345.678/0001-99;Z:\\FISCAL\\007 - Fulano\n", "cp1252"))
        self.assertEqual(problemas, [])
        self.assertEqual((clientes[0].nome, clientes[0].cnpj, clientes[0].num), ("Fulano Ltda", "12345678000199", "7"))
        self.assertEqual(clientes[0].pasta, r"Z:\FISCAL\007 - Fulano")

    def test_linhas_ruins_sao_separadas(self):
        clientes, problemas = bl.ler_clientes(csv_temporario(
            "nome;cnpj;pasta\n"
            "Bom;12.345.678/0001-99;Z:\\A\n"
            "Excel Estragou;1,23457E+13;Z:\\B\n"
            "Sem Pasta;98765432000100;\n"
            "Repetido;12345678000199;Z:\\C\n"))
        self.assertEqual([c.nome for c in clientes], ["Bom"])
        self.assertEqual({nome for _, nome, _ in problemas}, {"Excel Estragou", "Sem Pasta", "Repetido"})

    def test_zeros_a_esquerda_recompostos(self):
        clientes, _ = bl.ler_clientes(csv_temporario("nome;cnpj;pasta\nZeros;2345678000199;Z:\\A\n"))
        self.assertEqual(clientes[0].cnpj, "02345678000199")
        self.assertIn("recompostos", clientes[0].obs)

    def test_delimitador_virgula_e_colunas_opcionais(self):
        clientes, problemas = bl.ler_clientes(csv_temporario(
            "ativo,nome,cnpj,pasta,formato,thumbprint\n"
            "sim,A,12.345.678/0001-99,Z:\\A,pdf,\n"
            "nao,B,22.345.678/0001-99,Z:\\B,,\n"
            "sim,C,32.345.678/0001-99,Z:\\C,docx,\n"
            "sim,D,42.345.678/0001-99,Z:\\D,,a1b2c3d4 e5f6a7b8 c9d0e1f2 a3b4c5d6 e7f8a9b0\n"))
        self.assertEqual([(c.nome, c.ativo, c.formato) for c in clientes], [("A", True, "pdf"), ("B", False, ""), ("D", True, "")])
        self.assertEqual(clientes[2].thumbprint, "A1B2C3D4E5F6A7B8C9D0E1F2A3B4C5D6E7F8A9B0")
        self.assertEqual([n for _, n, _ in problemas], ["C"])

    def test_faltam_colunas(self):
        with self.assertRaises(ConfigError) as ctx:
            bl.ler_clientes(csv_temporario("a;b\n1;2\n"))
        self.assertIn("faltam as colunas", ctx.exception.mensagens[0])


class TestFiltro(unittest.TestCase):
    def setUp(self):
        self.lista = [bl.Cliente(2, "EXEMPLO COMERCIO LTDA", "12345678000199", "x", num="10"),
                      bl.Cliente(3, "OUTRA EMPRESA DE TESTE", "98765432000100", "y", num="20")]

    def nums(self, *termos):
        return [c.num for c in bl.filtrar_clientes(self.lista, list(termos))]

    def test_por_nome_sem_acento_ou_caixa(self):
        self.assertEqual(self.nums("exemplo comercio"), ["10"])

    def test_por_cnpj_formatado(self):
        self.assertEqual(self.nums("98.765.432/0001-00"), ["20"])

    def test_por_numero(self):
        self.assertEqual(self.nums("20"), ["20"])

    def test_sem_filtro_devolve_todos(self):
        self.assertEqual(self.nums(), ["10", "20"])


class TestControleNsu(unittest.TestCase):
    INI, FIM = date(2026, 10, 1), date(2026, 10, 31)

    def test_retoma_quando_o_periodo_esta_coberto(self):
        cob = bl.registrar_cobertura([], self.INI, self.FIM, 300, "A", None)
        self.assertEqual(bl.nsu_para_retomar(cob, self.INI, self.FIM, "A", None), 300)
        self.assertEqual(bl.nsu_para_retomar(cob, date(2026, 10, 5), date(2026, 10, 20), "A", None), 300)

    def test_nao_retoma_fora_da_cobertura(self):
        cob = bl.registrar_cobertura([], self.INI, self.FIM, 300, "A", None)
        casos = [
            (date(2026, 11, 1), date(2026, 11, 30), "A", None),   # mes seguinte
            (date(2026, 9, 1), date(2026, 10, 31), "A", None),    # periodo maior
            (self.INI, self.FIM, "A", "B"),                       # passou a querer PDF
            (self.INI, self.FIM, "OUTRA", None),                  # outra pasta de XML
        ]
        for ini, fim, xml, pdf in casos:
            self.assertEqual(bl.nsu_para_retomar(cob, ini, fim, xml, pdf), 0, (ini, fim, xml, pdf))

    def test_cobertura_maior_substitui_a_dominada(self):
        cob = bl.registrar_cobertura([], self.INI, self.FIM, 300, "A", None)
        cob = bl.registrar_cobertura(cob, self.INI, self.FIM, 350, "A", "B")
        self.assertEqual(len(cob), 1)
        self.assertEqual(bl.nsu_para_retomar(cob, self.INI, self.FIM, "A", None), 350)

    def test_periodos_diferentes_convivem(self):
        cob = bl.registrar_cobertura([], self.INI, self.FIM, 350, "A", None)
        cob = bl.registrar_cobertura(cob, date(2026, 11, 1), date(2026, 11, 30), 360, "A", None)
        self.assertEqual(len(cob), 2)
        self.assertEqual(bl.nsu_para_retomar(cob, self.INI, self.FIM, "A", None), 350)

    def test_estado_em_disco(self):
        nf.definir_pasta_dados(pasta_temp())
        self.addCleanup(nf.definir_pasta_dados, None)
        cob = bl.registrar_cobertura([], self.INI, self.FIM, 300, "A", None)
        bl.salvar_estado("12345678000199", cob)
        self.assertEqual(bl.carregar_estado("12345678000199"), cob)
        self.assertEqual(bl.carregar_estado("00000000000000"), [])
        bl.caminho_estado("11111111000111").parent.mkdir(parents=True, exist_ok=True)
        bl.caminho_estado("11111111000111").write_text("{lixo", encoding="utf-8")
        self.assertEqual(bl.carregar_estado("11111111000111"), [])  # estado corrompido e ignorado


class TestProcessarCliente(unittest.TestCase):
    INI, FIM = date(2026, 10, 1), date(2026, 10, 31)

    def setUp(self):
        self.dados = pasta_temp()
        self.pasta = pasta_temp()
        nf.definir_pasta_dados(self.dados)
        self.addCleanup(nf.definir_pasta_dados, None)
        self.cliente = bl.Cliente(2, "Cliente Teste", "12345678000199", str(self.pasta))
        self.padroes = bl.Padroes("mes_anterior", None, None, "xml", "", "Z:", "", False, 0, 0)

    def processar(self, resultado=None, erro=None, refazer=False, cert_erro=None):
        certificado = mock.patch.object(bl, "resolver_certificado", return_value=("T" * 40, "cert (valido ate 2030-01-01)"),
                                        side_effect=cert_erro)
        download = mock.patch.object(bl.nf, "executar_download", return_value=resultado, side_effect=erro)
        with certificado, download as chamada:
            linha = bl.processar_cliente(self.cliente, self.INI, self.FIM, self.padroes, refazer)
        return linha, chamada

    def test_ok_grava_cobertura(self):
        linha, _ = self.processar(ResultadoDownload(xml_novos=3, docs_vistos=10, ultimo_nsu=42, completo=True))
        self.assertEqual(linha.status, "OK")
        self.assertEqual(linha.validade, "2030-01-01")
        self.assertEqual(bl.carregar_estado(self.cliente.cnpj)[0]["nsu"], 42)

    def test_retoma_do_nsu_guardado(self):
        self.processar(ResultadoDownload(xml_novos=1, docs_vistos=1, ultimo_nsu=42, completo=True))
        linha, chamada = self.processar(ResultadoDownload(nsu_inicial=42, ultimo_nsu=42, docs_vistos=1, completo=True))
        self.assertEqual(chamada.call_args.kwargs["nsu_inicial"], 42)
        self.assertIn("sem notas novas", linha.obs)

    def test_refazer_ignora_o_estado(self):
        self.processar(ResultadoDownload(xml_novos=1, docs_vistos=1, ultimo_nsu=42, completo=True))
        _, chamada = self.processar(ResultadoDownload(completo=True, docs_vistos=1), refazer=True)
        self.assertEqual(chamada.call_args.kwargs["nsu_inicial"], 0)

    def test_nao_cria_a_pasta_base(self):
        _, chamada = self.processar(ResultadoDownload(completo=True, docs_vistos=1))
        self.assertIs(chamada.call_args.kwargs["criar_pasta_base"], False)

    def test_sem_documentos_e_sem_notas(self):
        linha, _ = self.processar(ResultadoDownload(completo=True, docs_vistos=0))
        self.assertEqual(linha.status, "SEM NOTAS")
        self.assertIn("procuracao", linha.obs)

    def test_incompleto_nao_grava_cobertura(self):
        linha, _ = self.processar(ResultadoDownload(completo=False, erro="curl 56", docs_vistos=5, ultimo_nsu=9))
        self.assertEqual((linha.status, linha.obs), ("INCOMPLETO", "curl 56"))
        self.assertEqual(bl.carregar_estado(self.cliente.cnpj), [])

    def test_com_erros_nao_grava_cobertura(self):
        linha, _ = self.processar(ResultadoDownload(completo=True, erros=2, docs_vistos=5, ultimo_nsu=9))
        self.assertEqual(linha.status, "COM ERROS")
        self.assertEqual(bl.carregar_estado(self.cliente.cnpj), [])

    def test_problema_de_configuracao_vira_erro_do_cliente(self):
        linha, _ = self.processar(erro=ConfigError(["pasta inacessivel"]))
        self.assertEqual((linha.status, linha.obs), ("ERRO", "pasta inacessivel"))

    def test_certificado_nao_encontrado_vira_erro_do_cliente(self):
        linha, chamada = self.processar(cert_erro=CertificadoNaoEncontrado(["sem certificado"]))
        self.assertEqual(linha.status, "ERRO")
        chamada.assert_not_called()

    def test_erro_inesperado_nao_derruba_o_lote(self):
        linha, _ = self.processar(erro=RuntimeError("boom"))
        self.assertEqual(linha.status, "ERRO")
        self.assertIn("boom", linha.obs)


class TestEntrada(unittest.TestCase):
    def executar(self, *argv):
        """Roda main() como se fosse o .exe (frozen) e devolve (codigo, vezes que pediu ENTER)."""
        pediu = []
        with mock.patch.object(sys, "argv", ["baixar_lote", *argv]), \
                mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(builtins, "input", lambda *_: pediu.append(1) or ""), \
                mock.patch.object(builtins, "print"), \
                mock.patch.object(sys, "stdout", io.StringIO()), \
                self.assertRaises(SystemExit) as saida:
            bl.main()
        return saida.exception.code, len(pediu)

    def test_versao_nao_espera_enter(self):
        self.assertEqual(self.executar("--versao"), (0, 0))

    def test_erro_de_configuracao_espera_enter_para_ler_a_mensagem(self):
        codigo, pediu = self.executar("--dados", str(pasta_temp() / "nao_existe"))
        self.assertEqual((codigo, pediu), (2, 1))


class TestCertificado(unittest.TestCase):
    def setUp(self):
        self.cliente = bl.Cliente(2, "C", "12345678000199", "x")
        self.padroes = bl.Padroes("mes_anterior", None, None, "xml", "", "Z:", "", False, 0, 0)

    def test_thumbprint_do_cliente_tem_prioridade(self):
        self.cliente.thumbprint = "A" * 40
        with mock.patch.object(bl, "_certificado_manual", return_value=("A" * 40, "manual")) as manual:
            self.assertEqual(bl.resolver_certificado(self.cliente, self.padroes), ("A" * 40, "manual"))
        manual.assert_called_once_with("A" * 40)

    def test_certificado_proprio_do_cliente(self):
        with mock.patch.object(bl.nf, "localizar_certificado", return_value=("B" * 40, "proprio")):
            self.assertEqual(bl.resolver_certificado(self.cliente, self.padroes)[1], "proprio")

    def test_reserva_de_procuracao(self):
        self.padroes.thumbprint_padrao = "C" * 40
        with mock.patch.object(bl.nf, "localizar_certificado", side_effect=CertificadoNaoEncontrado(["x"])), \
                mock.patch.object(bl, "_certificado_manual", return_value=("C" * 40, "e-CPF")):
            thumbprint, descricao = bl.resolver_certificado(self.cliente, self.padroes)
        self.assertEqual(thumbprint, "C" * 40)
        self.assertIn("procuracao", descricao)

    def test_sem_reserva_o_erro_sobe(self):
        with mock.patch.object(bl.nf, "localizar_certificado", side_effect=CertificadoNaoEncontrado(["x"])):
            with self.assertRaises(CertificadoNaoEncontrado):
                bl.resolver_certificado(self.cliente, self.padroes)


if __name__ == "__main__":
    unittest.main()
