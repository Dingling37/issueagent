"""Check the build-selected Java compiler before any Agent/model is created."""
import json
import os
from pathlib import Path
import re
import subprocess
from time import monotonic

from ..tools.test import TestRunner, decode_test_output
from ..workspace.workspace import Workspace


GRADLE_PROBE = r"""
gradle.projectsEvaluated {
    rootProject.tasks.register('issueAgentCompilerPreflight') {
        doLast {
            def compilers = []
            rootProject.allprojects.each { p ->
                p.tasks.withType(org.gradle.api.tasks.compile.JavaCompile).each { t ->
                    compilers.add(t.javaCompiler.get().executablePath.asFile.absolutePath)
                }
            }
            if (compilers.isEmpty()) throw new GradleException('No Java compiler tasks found')
            compilers.unique().each { println('ISSUE_AGENT_JAVAC=' + groovy.json.JsonOutput.toJson(it)) }
        }
    }
}
"""


def build_probe_command(workspace: Workspace, arguments: list[str]) -> list[str] | str:
    # Reuse TestRunner's wrapper discovery and Windows executable quoting.
    runner = TestRunner.__new__(TestRunner)
    runner.workspace = workspace
    command = runner.command()
    if os.name == 'nt' and command[1:4] == ['/d', '/s', '/c']:
        prefix = command[4].split('" ', 1)[0]
        if any(any(c in arg for c in '&|<>^%!"\r\n') for arg in arguments):
            raise ValueError('Unsupported character in preflight arguments')
        tail = ' '.join('"' + arg + '"' if ' ' in arg else arg for arg in arguments)
        return subprocess.list2cmdline(command[:4]) + ' ' + prefix + '" ' + tail + '"'
    prefix = command[:2] if command[0] == '/bin/sh' else command[:1]
    return [*prefix, *arguments]


def _execute(command: list[str] | str, workspace: Workspace, log: Path, timeout: int) -> tuple[int, str]:
    environment = {k: v for k, v in os.environ.items()
                   if not any(marker in k.upper() for marker in ('API_KEY', 'AUTH_TOKEN'))}
    with log.open('wb') as stream:
        process = subprocess.Popen(command, cwd=workspace.root, env=environment,
            stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
            start_new_session=os.name != 'nt')
        try:
            process.wait(timeout=timeout)
        except BaseException:
            TestRunner.stop(process)
            raise
    if log.stat().st_size > 1_000_000:
        raise ValueError('Preflight output exceeded 1 MB')
    return process.returncode, decode_test_output(log.read_bytes())


def compiler_paths(output: str, gradle: bool) -> list[str]:
    output = re.sub(r"\x1b\[[0-9;]*m", "", output)
    if gradle:
        paths = [json.loads(line.split('=', 1)[1]) for line in output.splitlines()
                 if line.startswith('ISSUE_AGENT_JAVAC=')]
    else:
        match = re.search(r'(?im)^.*?Java home:\s*(.+?)\s*$', output)
        if not match:
            match = re.search(r'(?im)^.*?Java version:.*?runtime:\s*(.+?)\s*$', output)
        if not match:
            return []
        home = Path(match.group(1).strip())
        if home.name.lower() == 'jre': home = home.parent
        paths = [str(home / 'bin' / ('javac.exe' if os.name == 'nt' else 'javac'))]
    return list(dict.fromkeys(paths))


def run_preflight(workspace: Workspace, output_dir: Path, timeout: int = 60) -> dict:
    started = monotonic()
    result = {'passed': False, 'phase': 'java_compiler_preflight', 'compiler_paths': [],
              'error': None, 'log_path': str(output_dir / 'preflight.log')}
    try:
        gradle = not workspace.resolve('pom.xml').is_file()
        if gradle:
            init = output_dir / 'preflight.init.gradle'
            init.write_text(GRADLE_PROBE, encoding='utf-8')
            arguments = ['--init-script', str(init), 'issueAgentCompilerPreflight', '--no-daemon']
        else:
            arguments = ['-version']
        command = build_probe_command(workspace, arguments)
        code, output = _execute(command, workspace, output_dir / 'preflight.log', timeout)
        result['returncode'] = code
        if code:
            raise ValueError('Build environment probe failed: ' + output[-2000:])
        paths = compiler_paths(output, gradle)
        if not paths:
            raise ValueError('Build tool did not report a Java compiler')
        for index, path in enumerate(paths):
            if not isinstance(path, str) or not Path(path).is_file():
                raise ValueError('JAVA_COMPILER executable missing: ' + str(path))
            code, version = _execute([path, '-version'], workspace,
                                     output_dir / f'preflight-javac-{index}.log', timeout)
            if code or not re.search(r'\bjavac\s+\d', version):
                raise ValueError('JAVA_COMPILER capability check failed: ' + version[-1000:])
        result['compiler_paths'] = paths
        result['passed'] = True
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
    result['elapsed_seconds'] = monotonic() - started
    return result
