import ast
import contextlib
import io
import re
import shlex
import subprocess
import sys
import tempfile
import traceback
from typing import List, Optional, Union
from unittest.mock import patch

import pyp
import pytest

# ====================
# Helpers
# ====================


@pytest.fixture(autouse=True)
def delete_config_env_var(monkeypatch):
    monkeypatch.delenv("PYP_CONFIG_PATH", raising=False)


def run_cmd(cmd: str, input: Optional[str] = None, check: bool = True) -> str:
    if isinstance(input, str):
        input = input.encode("utf-8")
    proc = subprocess.run(
        cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=check, input=input
    )
    return proc.stdout.decode("utf-8")


def run_pyp(cmd: Union[str, List[str]], input: Optional[str] = None) -> str:
    """Run pyp in process. It's quicker and allows us to mock and so on."""
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    if cmd[0] == "pyp":
        del cmd[0]

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        try:
            old_stdin = sys.stdin
            sys.stdin = io.StringIO(input)
            pyp.run_pyp(pyp.parse_options(cmd))
        finally:
            sys.stdin = old_stdin
    return output.getvalue()


def compare_command(
    example_cmd: str, pyp_cmd: str, input: Optional[str] = None, use_subprocess: bool = False
) -> None:
    """Compares running command example_cmd with the output of pyp_cmd.

    ``use_subprocess`` tells us whether to launch pyp via subprocess or not.

    """
    pyp_output = run_cmd(pyp_cmd, input) if use_subprocess else run_pyp(pyp_cmd, input)
    assert run_cmd(example_cmd, input) == pyp_output


def compare_scripts(explain_output: str, script: str) -> None:
    """Tests whether two scripts are equivalent."""
    explain_output = explain_output.strip("\n")
    script = script.strip("\n")
    if sys.version_info < (3, 9):
        # astunparse seems to parenthesise things slightly differently, so filter through ast to
        # hackily ensure that the scripts are the same.
        assert pyp.unparse(ast.parse(explain_output)) == pyp.unparse(ast.parse(script))
    else:
        assert explain_output == script


# ====================
# Tests
# ====================


def test_examples():
    """Test approximately the examples in the README."""

    example = "python\nsnake\nss\nmagpy\npiethon\nreadme.md\n"

    compare_command(example_cmd="cut -c1-4", pyp_cmd="pyp 'x[:4]'", input=example)
    compare_command(
        example_cmd="wc -c | tr -d ' '", pyp_cmd="pyp 'len(stdin.read())'", input=example
    )
    compare_command(
        example_cmd="awk '{s+=$1} END {print s}' ",
        pyp_cmd="pyp 'sum(map(int, lines))'",
        input="1\n2\n3\n4\n5\n",
    )
    compare_command(
        example_cmd="sh",
        pyp_cmd="pyp 'subprocess.run(lines[0], shell=True); pass'",
        input="echo echo",
        use_subprocess=True,
    )
    compare_command(
        example_cmd="""jq -r '.[1]["lol"]'""",
        pyp_cmd="""pyp 'json.load(stdin)[1]["lol"]'""",
        input='[0, {"lol": "hmm"}, 0]',
    )
    compare_command(
        example_cmd="grep -E '(py|md)'",
        pyp_cmd="""pyp 'x if re.search("(py|md)", x) else None'""",
        input=example,
    )
    compare_command(example_cmd="echo 'sqrt(9.0)' | bc", pyp_cmd="pyp 'sqrt(9)'")
    compare_command(
        example_cmd="x=README.md; echo ${x##*.}", pyp_cmd='''pyp "Path('README.md').suffix[1:]"''',
    )
    compare_command(
        example_cmd="echo '  1 a\n  2 b'", pyp_cmd="""pyp 'f"{idx+1: >3} {x}"'""", input="a\nb"
    )
    compare_command(
        example_cmd="grep py", pyp_cmd="""pyp 'x if "py" in x else None'""", input=example
    )
    compare_command(example_cmd="tail -n 3", pyp_cmd="pyp 'lines[-3:]'", input=example)
    compare_command(example_cmd="sort", pyp_cmd="pyp 'sorted(lines)'", input=example)
    compare_command(
        example_cmd="echo 'Sorting 2 lines\n1\n2'",
        pyp_cmd="""pyp 'print(f"Sorting {len(lines)} lines"); pypprint(sorted(lines))'""",
        input="2\n1\n",
    )
    compare_command(
        example_cmd="sort | uniq", pyp_cmd="pyp 'sorted(set(lines))'", input="2\n1\n1\n1\n3"
    )
    compare_command(
        example_cmd='''echo "a: ['1', '3']\nb: ['2']"''',
        pyp_cmd="pyp -b 'd = defaultdict(list)' 'k, v = x.split(); d[k].append(v)' -a 'd'",
        input="a 1\nb 2\na 3",
    )


def test_magic_variable_failures():
    with pytest.raises(pyp.PypError, match="Code doesn't generate any output"):
        run_pyp("pyp 'x = 1'")

    with pytest.raises(pyp.PypError, match="Candidates found for both"):
        run_pyp("pyp 'print(x); print(len(lines))'")

    with pytest.raises(pyp.PypError, match="Multiple candidates for loop"):
        run_pyp("pyp 'print(x); print(s)'")

    with pytest.raises(pyp.PypError, match="Multiple candidates for input"):
        run_pyp("pyp 'stdin; lines'")


def test_user_error():
    pattern = re.compile("Invalid input.*SyntaxError", re.DOTALL)
    with pytest.raises(pyp.PypError, match="Invalid input"):
        run_pyp("pyp 'x +'")

    pattern = re.compile("Code raised.*Possible.*1 / 0.*ZeroDivisionError", re.DOTALL)
    with pytest.raises(pyp.PypError, match=pattern):
        run_pyp("pyp '1 / 0'")

    # Test the special cased error messages
    pattern = re.compile(
        "Code raised.*Possible.*import lol.*ModuleNotFoundError.*forgot.*PYP_CONFIG_PATH", re.DOTALL
    )
    with pytest.raises(pyp.PypError, match=pattern):
        run_pyp("pyp 'lol'")

    pattern = re.compile(
        "Code raised.*Possible.*lines.*NameError.*--before.*before any magic variables", re.DOTALL
    )
    with pytest.raises(pyp.PypError, match=pattern):
        run_pyp("pyp -b 'lines = map(int, lines)' 'sum(lines)'")


def test_tracebacks():
    # If our sins against traceback implementation details come back to haunt us, and we can't
    # reconstruct a traceback, check that we still output something reasonable
    TBE = traceback.TracebackException
    with patch("traceback.TracebackException") as mock_tb:
        count = 0

        def effect(*args, **kwargs):
            nonlocal count
            if count == 0:
                assert args[0] == ZeroDivisionError
                count += 1
                raise Exception
            return TBE(*args, **kwargs)

        mock_tb.side_effect = effect
        pattern = re.compile("Code raised.*ZeroDivisionError", re.DOTALL)
        with pytest.raises(pyp.PypError, match=pattern) as e:
            run_pyp("pyp '1 / 0'")
        # Make sure that the test works and we couldn't actually reconstruct a traceback
        assert "Possible" not in e.value.args[0]

    # Check the entire output, end to end
    pyp_error = run_cmd("pyp 'def f(): 1/0' 'f()'", check=False)
    message = lambda x, y: (  # noqa
        "error: Code raised the following exception, consider using --explain to investigate:\n\n"
        "Possible reconstructed traceback (most recent call last):\n"
        '  File "<pyp>", in <module>\n'
        "    output = f()\n"
        '  File "<pyp>", in f\n'
        f"    {x}1 / 0{y}\n"
        "ZeroDivisionError: division by zero\n"
    )
    assert pyp_error == message("(", ")") or pyp_error == message("", "")

    # Test tracebacks involving statements with nested child statements
    pyp_error = run_cmd("""pyp 'if 1 / 0: print("should_not_get_here")'""", check=False)
    assert "should_not_get_here" not in pyp_error


def test_explain():
    command = (
        "pyp --explain -b 'd = defaultdict(list)' 'user, pid, *_ = x.split()' "
        """'d[user].append(pid)' -a 'del d["root"]' -a d"""
    )
    script = r"""
#!/usr/bin/env python3
from collections import defaultdict
import sys
from pyp import pypprint
d = defaultdict(list)
for x in sys.stdin:
    x = x.rstrip('\n')
    (user, pid, *_) = x.split()
    d[user].append(pid)
del d['root']
if d is not None:
    pypprint(d)
"""
    compare_scripts(run_pyp(command), script)


def test_disable_automatic_print():
    assert run_pyp("pyp pass") == ""
    assert run_pyp("pyp '1; pass'") == ""
    assert run_pyp("pyp 'print(1)'") == "1\n"
    assert run_pyp("pyp 'print(1)'; 2") == "1\n"


def test_automatic_print_inside_statement():
    assert run_pyp("pyp 'if int(x) > 2: x'", input="1\n4\n2\n3") == "4\n3\n"
    assert run_pyp("pyp 'if int(x) > 2:' ' if int(x) < 4: x'", input="1\n4\n2\n3") == "3\n"
    assert run_pyp("pyp 'with contextlib.suppress(): x'", input="a\nb") == "a\nb\n"
    with pytest.raises(pyp.PypError, match="Code doesn't generate any output"):
        run_pyp("pyp 'if int(x) > 2: int(x)' 'else: int(x) + 1'")


def test_pypprint_basic():
    assert run_pyp("pyp 'pypprint(1); pypprint(1, 2)'") == "1\n1 2\n"
    assert run_pyp("pyp i", input="a\nb") == "0\n1\n"
    assert run_pyp("pyp --define-pypprint lines", input="a\nb") == "a\nb\n"


def test_get_valid_name():
    # output is already defined, so we shouldn't print 0
    assert run_pyp("pyp 'output = 0; 1'") == "1\n"
    # we shouldn't try to define output, so it should fall through as an automatic import and fail
    with pytest.raises(Exception) as e:
        run_pyp("pyp 'output.foo()'")
    assert isinstance(e.value.__cause__, ImportError)


# ====================
# Config tests
# ====================


@patch("pyp.get_config_contents")
def test_config_imports(config_mock):
    config_mock.return_value = """
import numpy as np
from scipy.linalg import eigvals
from contextlib import *
from typing import *

def smallarray():
    return np.array([0])
    """
    script1 = """
#!/usr/bin/env python3
import sys
import numpy as np
stdin = sys.stdin
stdin
np
"""
    compare_scripts(run_pyp(["--explain", "stdin; np; pass"]), script1)

    script2 = """
#!/usr/bin/env python3
import sys
import numpy as np
from scipy.linalg import eigvals
assert sys.stdin.isatty() or not sys.stdin.read(), "The command doesn't process input, but input is present"
eigvals(np.array([[0.0, -1.0], [1.0, 0.0]]))
"""  # noqa
    compare_scripts(
        run_pyp(["--explain", "eigvals(np.array([[0., -1.], [1., 0.]])); pass"]), script2
    )

    script3 = r"""
#!/usr/bin/env python3
from contextlib import suppress
from typing import List
import sys
for x in sys.stdin:
    x = x.rstrip('\n')
    y: List = []
    with suppress():
        if x is not None:
            print(x)
"""
    compare_scripts(run_pyp(["--explain", "y: List = []", "with suppress(): x"]), script3)

    script4 = """
#!/usr/bin/env python3
import sys
import numpy as np

def smallarray():
    return np.array([0])
stdin = sys.stdin
stdin
smallarray()
"""
    compare_scripts(run_pyp(["--explain", "stdin; smallarray(); pass"]), script4)


@patch("pyp.get_config_contents")
def test_config_invalid(config_mock):
    config_mock.return_value = "import numpy as np\nimport scipy as np"
    with pytest.raises(pyp.PypError, match="Config has multiple definitions"):
        run_pyp("x")

    config_mock.return_value = "from . import asdf"
    with pytest.raises(pyp.PypError, match="Config has unsupported import"):
        run_pyp("x")

    config_mock.return_value = "f()"
    with pytest.raises(pyp.PypError, match=r"Config.*unsupported construct \(call\)"):
        run_pyp("x")

    config_mock.return_value = "del x"
    with pytest.raises(pyp.PypError, match=r"Config.*unsupported construct \(delete\)"):
        run_pyp("x")

    config_mock.return_value = "1 +"
    with pytest.raises(pyp.PypError, match="Config has invalid syntax"):
        run_pyp("x")

    config_mock.return_value = "from xyz import *"
    run_pyp("x")
    with pytest.raises(pyp.PypError, match="Config.*wildcard import.*xyz.*failed") as e:
        run_pyp("missing")
    assert isinstance(e.value.__cause__, ImportError)


@patch("pyp.get_config_contents")
def test_config_shebang(config_mock):
    config_mock.return_value = "#!hello"
    script = """
#!hello
import sys
stdin = sys.stdin
stdin
"""
    compare_scripts(run_pyp(["--explain", "stdin; pass"]), script)


@patch("pyp.get_config_contents")
def test_config_lazy_wildcard_import(config_mock):
    # importing "this" has a side effect, so we can tell whether or not it was imported
    config_mock.return_value = "from this import *"
    assert run_pyp("pass") == ""  # not imported
    assert run_pyp("x[:3]") == ""  # not imported
    assert run_pyp("lines") == ""  # not imported
    assert "Zen of Python" in run_pyp("asyncio")  # imported


@patch("pyp.get_config_contents")
def test_config_scope(config_mock):
    config_mock.return_value = """
def f(x): contextlib = 5
class A:
    def asyncio(self): ...
"""
    script = """
#!/usr/bin/env python3
import asyncio
import contextlib
import sys
stdin = sys.stdin
stdin
contextlib
asyncio
"""
    compare_scripts(run_pyp(["--explain", "stdin; contextlib; asyncio; pass"]), script)


@patch("pyp.get_config_contents")
def test_config_shadow(config_mock):
    # shadowing a builtin
    config_mock.return_value = "range = 5"
    assert run_pyp("print(range)") == "5\n"

    # shadowing a magic variable
    config_mock.return_value = "stdin = 5"
    assert run_pyp("type(stdin).__name__") == "StringIO\n"
    config_mock.return_value = "x = 8"
    assert run_pyp("x") == ""

    # shadowing a wildcard import
    config_mock.return_value = "from typing import *\nList = 5"
    assert run_pyp("List") == "5\n"

    # shadowing another wildcard import
    config_mock.return_value = "from os.path import *\nfrom shlex import *"
    assert run_pyp("split.__module__") == "shlex\n"


@patch("pyp.get_config_contents")
def test_config_recursive(config_mock):
    config_mock.return_value = "def f(x): return g(x)\ndef g(x): return f(x)"
    script = """
#!/usr/bin/env python3
import sys

def f(x):
    return g(x)

def g(x):
    return f(x)
stdin = sys.stdin
stdin
f(1)
"""
    compare_scripts(run_pyp(["--explain", "stdin; f(1); pass"]), script)


@patch("pyp.get_config_contents")
def test_config_conditional(config_mock):
    if_block = """\
if sys.version_info < (3, 9):
    import astunparse
    unparse = astunparse.unparse
else:
    unparse = ast.unparse\
"""
    config_mock.return_value = f"import sys\n{if_block}"
    script1 = f"""
#!/usr/bin/env python3
import ast
import sys
{if_block}
assert sys.stdin.isatty() or not sys.stdin.read(), "The command doesn't process input, but input is present"
unparse(ast.parse('x'))
"""  # noqa
    compare_scripts(run_pyp(["--explain", "unparse(ast.parse('x')); pass"]), script1)

    except_block = """\
try:
    import astunparse
    unparse = astunparse.unparse
except ImportError:
    import ast
    unparse = ast.unparse\
"""
    config_mock.return_value = except_block
    script2 = f"""
#!/usr/bin/env python3
import ast
import sys
{except_block}
assert sys.stdin.isatty() or not sys.stdin.read(), "The command doesn't process input, but input is present"
unparse(ast.parse('x'))
"""  # noqa
    compare_scripts(run_pyp(["--explain", "unparse(ast.parse('x')); pass"]), script2)


def test_config_end_to_end(monkeypatch):
    with tempfile.NamedTemporaryFile("w") as f:
        monkeypatch.setenv("PYP_CONFIG_PATH", f.name)
        config = "def foo(): return 1"
        f.write(config)
        f.flush()
        assert run_pyp("foo()") == "1\n"

        monkeypatch.setenv("PYP_CONFIG_PATH", f.name + "_does_not_exist")
        with pytest.raises(pyp.PypError, match=f"Config file not found.*{f.name}"):
            run_pyp("foo()")
