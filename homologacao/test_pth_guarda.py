import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest
import venv
from pathlib import Path
from unittest import mock

from homologacao import guarda, pth_guarda

_RAIZ = Path(__file__).resolve().parent.parent
_PTH = _RAIZ / "infra" / "homolog" / "homolog-guarda.pth"
_RAIZ_VPS = "'/opt/stokki-homolog'"


class TestAtivar(unittest.TestCase):
    def setUp(self):
        self.raiz = "/raiz-de-teste-pth"
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("FRESH_AMBIENTE", None)
        os.environ.pop(pth_guarda.VARIAVEL_PIP, None)
        self.instalar = mock.patch.object(guarda, "instalar").start()
        self.saida = mock.patch.object(pth_guarda.os, "_exit").start()
        self.addCleanup(mock.patch.stopall)
        sys.path.insert(0, self.raiz)
        self.addCleanup(lambda: self.raiz in sys.path and sys.path.remove(self.raiz))

    def test_instala_a_guarda_e_marca_o_ambiente(self):
        pth_guarda.ativar(self.raiz)
        self.instalar.assert_called_once_with()
        self.assertEqual(os.environ["FRESH_AMBIENTE"], "homolog")
        self.assertNotIn(self.raiz, sys.path)
        self.saida.assert_not_called()

    def test_forca_homolog_mesmo_com_outro_valor(self):
        os.environ["FRESH_AMBIENTE"] = "producao"
        pth_guarda.ativar(self.raiz)
        self.assertEqual(os.environ["FRESH_AMBIENTE"], "homolog")
        self.instalar.assert_called_once_with()

    def test_falha_na_instalacao_aborta_o_processo(self):
        self.instalar.side_effect = RuntimeError("camada quebrada")
        with mock.patch.object(pth_guarda.sys, "stderr") as erro:
            pth_guarda.ativar(self.raiz)
        self.saida.assert_called_once_with(1)
        self.assertIn("camada quebrada", erro.write.call_args.args[0])

    def test_pip_do_root_pula_a_guarda(self):
        os.environ[pth_guarda.VARIAVEL_PIP] = "1"
        with mock.patch.object(pth_guarda.os, "geteuid", create=True, return_value=0):
            pth_guarda.ativar(self.raiz)
        self.instalar.assert_not_called()
        self.assertNotIn("FRESH_AMBIENTE", os.environ)
        self.assertNotIn(self.raiz, sys.path)

    def test_variavel_do_pip_nao_vale_fora_do_root(self):
        os.environ[pth_guarda.VARIAVEL_PIP] = "1"
        with mock.patch.object(pth_guarda.os, "geteuid", create=True, return_value=999):
            pth_guarda.ativar(self.raiz)
        self.instalar.assert_called_once_with()

    def test_root_sem_a_variavel_tem_guarda(self):
        with mock.patch.object(pth_guarda.os, "geteuid", create=True, return_value=0):
            pth_guarda.ativar(self.raiz)
        self.instalar.assert_called_once_with()


class TestPthNoVenv(unittest.TestCase):
    """venv de verdade (sem pip, offline) com o .pth do repo apontando para esta raiz."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        base = Path(cls.dir.name)
        venv.EnvBuilder(with_pip=False).create(base / "v")
        pasta = "Scripts" if os.name == "nt" else "bin"
        cls.python = str(base / "v" / pasta / ("python.exe" if os.name == "nt" else "python"))
        cls.site = Path(subprocess.run([cls.python, "-I", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                                       capture_output=True, text=True, check=True, timeout=60).stdout.strip())
        # requests/urllib3 do python dos testes; aa-* vem antes de homolog-* (o site.py le os .pth em ordem)
        # linha import com ascii(): o site.py le o .pth na codificacao local (com -E/-I, cp1252 no Windows)
        (cls.site / "aa-base.pth").write_text("import sys; sys.path.append(%s)\n" % ascii(sysconfig.get_paths()["purelib"]),
                                              encoding="ascii")
        cls.banco = base / "dados.db"
        cls.env = {k: v for k, v in os.environ.items()
                   if k not in ("FRESH_AMBIENTE", "PYTHONPATH", "PYTHONHOME", pth_guarda.VARIAVEL_PIP)}
        cls.env["HOMOLOG_REGISTRO_BANCO"] = str(cls.banco)

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def _instalar_pth(self, raiz):
        texto = _PTH.read_text(encoding="ascii")
        self.assertEqual(texto.count(_RAIZ_VPS), 2)
        (self.site / "homolog-guarda.pth").write_text(texto.replace(_RAIZ_VPS, repr(Path(raiz).as_posix())), encoding="ascii")
        self.addCleanup(lambda: (self.site / "homolog-guarda.pth").unlink())

    def _rodar(self, *flags, codigo):
        return subprocess.run([self.python, *flags, "-c", codigo], env=self.env, cwd=self.dir.name,
                              capture_output=True, text=True, timeout=120)

    def test_guarda_com_e_sem_flags_de_isolamento(self):
        self._instalar_pth(_RAIZ)
        codigo = ("import os, smtplib, sys; from homologacao import guarda\n"
                  "print(smtplib.SMTP.__name__, os.environ.get('FRESH_AMBIENTE'), ','.join(guarda.CAMADAS),"
                  " %r in sys.path)" % _RAIZ.as_posix())
        for flags in ((), ("-E",), ("-I",), ("-E", "-s")):
            with self.subTest(flags=flags):
                r = self._rodar(*flags, codigo=codigo)
                self.assertEqual(r.returncode, 0, r.stderr)
                smtp, ambiente, camadas, raiz_no_path = r.stdout.split()
                self.assertEqual((smtp, ambiente), ("SMTPFalso", "homolog"))
                self.assertIn("requests", camadas.split(","))
                self.assertIn("socket", camadas.split(","))
                self.assertEqual(raiz_no_path, "False")

    def test_menos_s_pula_o_pth(self):
        # unico jeito de fugir do .pth: sem o modulo site (sem site-packages); o nft segura
        self._instalar_pth(_RAIZ)
        r = self._rodar("-S", codigo="import smtplib, sys; print(smtplib.SMTP.__name__, 'homologacao' in sys.modules)")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ["SMTP", "False"])

    def test_raiz_sem_a_guarda_aborta(self):
        vazia = Path(self.dir.name) / "sem-homologacao"
        vazia.mkdir(exist_ok=True)
        self._instalar_pth(vazia)
        for flags in ((), ("-I",)):
            with self.subTest(flags=flags):
                r = self._rodar(*flags, codigo="print('SEGUIU')")
                self.assertEqual(r.returncode, 1)
                self.assertNotIn("SEGUIU", r.stdout)
                self.assertIn("guarda de rede nao carregou", r.stderr)

    def test_guarda_que_falha_aborta(self):
        # raiz com um homologacao/guarda.py que levanta: ativar() derruba o processo
        raiz = Path(self.dir.name) / "guarda-quebrada"
        (raiz / "homologacao").mkdir(parents=True, exist_ok=True)
        shutil.copy(_RAIZ / "homologacao" / "pth_guarda.py", raiz / "homologacao" / "pth_guarda.py")
        (raiz / "homologacao" / "__init__.py").write_text("", encoding="utf-8")
        (raiz / "homologacao" / "guarda.py").write_text(
            "def instalar():\n    raise RuntimeError('quebrada de proposito')\n", encoding="utf-8")
        self._instalar_pth(raiz)
        r = self._rodar("-I", codigo="print('SEGUIU')")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("SEGUIU", r.stdout)
        self.assertIn("quebrada de proposito", r.stderr)


if __name__ == "__main__":
    unittest.main()
