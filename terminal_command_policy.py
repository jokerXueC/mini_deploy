"""Conservative argv rules for automatic SSH commands; unknown programs need review."""
from __future__ import annotations

import re
import shlex


DESTRUCTIVE = {'rm', 'rmdir', 'unlink', 'shred', 'truncate', 'dd', 'wipefs', 'blkdiscard',
               'lvremove', 'vgremove', 'pvremove', 'userdel', 'groupdel', 'deluser', 'delgroup'}
READERS = {'ls', 'cat', 'head', 'tail', 'wc', 'stat', 'df', 'du', 'free', 'uptime', 'uname',
           'id', 'whoami', 'ps', 'grep'}
IDENT = r'(?:[A-Za-z_][A-Za-z_0-9]*|"[A-Za-z_][A-Za-z_0-9]*")'
TABLE = IDENT + r'(?:\.' + IDENT + r')?'
PROJECTION = r'(?:\*|' + IDENT + r'(?:\s*,\s*' + IDENT + r')*|COUNT\s*\(\s*\*\s*\)(?:\s+AS\s+' + IDENT + r')?)'
SIMPLE_SELECT = re.compile(r'\s*SELECT\s+' + PROJECTION + r'\s+FROM\s+' + TABLE +
                          r'(?:\s+LIMIT\s+[0-9]{1,7}(?:\s+OFFSET\s+[0-9]{1,7})?)?\s*;?\s*', re.I)


def _base(word):
    return word.rsplit('/', 1)[-1]


def _tokens(command):
    lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>()\n')
    lexer.whitespace = ' \t\r'
    lexer.whitespace_split = True
    lexer.commenters = '#'
    return list(lexer)


def _sql_deletes(sql):
    # Only for identifying explicit destruction. Auto execution uses a full-match grammar below.
    words = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|--[^\n]*|/\*.*?\*/", ' ', sql, flags=re.S)
    return bool(re.search(r'\b(?:DELETE|DROP|TRUNCATE)\b', words, re.I))


def _payload(words):
    while words and (_base(words[0]) in {'sudo', 'command', 'exec', 'env'} or
                     re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*=.*', words[0])):
        wrapper, words = _base(words[0]), words[1:]
        while words and words[0].startswith('-'):
            option, words = words[0], words[1:]
            if option == '--':
                break
            if (wrapper == 'sudo' and option in {'-u', '--user', '-g', '--group', '-h', '--host', '-p', '--prompt'} or
                    wrapper == 'env' and option in {'-u', '--unset', '-C', '--chdir'}):
                words = words[1:]
    return words


def _docker_exec_payload(words):
    index = 2
    while index < len(words) and words[index].startswith('-'):
        option = words[index]
        index += 1
        if option == '--':
            break
        if option in {'-u', '--user', '-w', '--workdir', '-e', '--env', '--env-file', '--detach-keys'}:
            index += 1
    return words[index + 1:]


def _destructive(tokens, depth=0):
    if depth > 4:
        return False
    for index, token in enumerate(tokens):
        if index and tokens[index - 1] not in {';', '&&', '||', '|', '\n', '(', ')', '&'}:
            continue
        words = _payload(tokens[index:])
        if not words:
            continue
        name = _base(words[0])
        if '>' in words:
            prefix = words[:words.index('>')]
            if not prefix or prefix in ([':'], ['true'], ['cat', '/dev/null'], ['echo', ''], ['printf', '']):
                return True
        if name in DESTRUCTIVE or name.startswith(('mkfs', 'mkswap')):
            return True
        if name == 'find' and any(w in {'-delete', '-exec', '-execdir'} for w in words[1:]):
            if '-delete' in words or any(_base(w) in DESTRUCTIVE for w in words[1:]):
                return True
        if name == 'docker':
            if len(words) > 1 and words[1] in {'rm', 'rmi', 'prune'}:
                return True
            if len(words) > 2 and words[1] in {'container', 'image', 'volume', 'network', 'system', 'builder', 'buildx'}:
                if words[2] in {'rm', 'prune'}:
                    return True
            if len(words) > 2 and words[1] in {'compose', 'exec'}:
                if words[1] == 'compose' and any(w in {'down', 'rm'} for w in words[2:]):
                    return True
                if words[1] == 'exec' and _destructive(_docker_exec_payload(words), depth + 1):
                    return True
        if name in {'apt', 'apt-get', 'yum', 'dnf', 'snap', 'pip', 'pip3'} and any(
                w in {'remove', 'purge', 'autoremove', 'erase', 'uninstall'} for w in words[1:]):
            return True
        if name in {'psql', 'mysql', 'mariadb', 'sqlite3'} and any(_sql_deletes(w) for w in words[1:]):
            return True
        if name in {'sh', 'bash', 'dash', 'zsh'}:
            for arg, script in zip(words[1:], words[2:]):
                if re.fullmatch(r'-[A-Za-z]*c[A-Za-z]*', arg):
                    try:
                        if _destructive(_tokens(script), depth + 1):
                            return True
                    except ValueError:
                        pass
        if name.startswith(('python', 'perl', 'ruby', 'node')) and any(re.search(
                r'\b(?:unlink|rmtree|rmdir|remove|rmSync|unlinkSync)\s*\(', word) for word in words[1:]):
            return True
    return False


def _psql(words):
    args, query, index = [], None, 1
    while index < len(words):
        arg = words[index]
        if arg in {'-X', '--no-psqlrc', '-A', '-t', '-q', '--csv'}:
            args.append(arg)
            index += 1
        elif arg in {'-U', '--username', '-d', '--dbname', '-h', '--host', '-p', '--port'}:
            if index + 1 >= len(words) or not re.fullmatch(r'[A-Za-z0-9_.-]+', words[index + 1]):
                return None
            args.extend(words[index:index + 2])
            index += 2
        elif arg in {'-c', '--command'} and query is None and index + 1 < len(words):
            query = words[index + 1]
            index += 2
        else:
            return None
    if query is None or not SIMPLE_SELECT.fullmatch(query):
        return None
    # -X prevents a user psqlrc from running arbitrary startup commands.
    # Separate actions keep the SELECT result visible; a final ROLLBACK in one
    # command string can otherwise hide the query result in psql.
    return [words[0], '-X', '-v', 'ON_ERROR_STOP=1', *args,
            '-c', 'BEGIN READ ONLY;', '-c', query.strip(), '-c', 'ROLLBACK;']


def _read_options(words, flags, values, positional):
    index = 0
    while index < len(words):
        word = words[index]
        if word in flags:
            index += 1
        elif word in values and index + 1 < len(words) and not words[index + 1].startswith('-'):
            index += 2
        elif '=' in word and word.split('=', 1)[0] in values:
            index += 1
        elif not word.startswith('-') and re.fullmatch(positional, word):
            index += 1
        else:
            return False
    return True


def _read_command(words):
    if not words:
        return None
    name = _base(words[0])
    if words[0] not in {name, '/usr/bin/' + name, '/bin/' + name, '/usr/sbin/' + name, '/sbin/' + name}:
        return None
    if name in READERS or name == 'pwd' and len(words) <= 2:
        return words
    if name == 'hostname' and words[1:] in ([], ['-f'], ['-I'], ['-i']):
        return words
    if name == 'ss' and _read_options(words[1:], {'-a', '-l', '-n', '-t', '-u', '-p', '-s', '-r', '-e', '-i', '-o',
                                                '--all', '--listening', '--numeric', '--tcp', '--udp', '--processes',
                                                '--summary', '--extended', '--info', '--options'}, set(), r'(?!)'):
        return words
    if name == 'ss' and len(words) == 2 and re.fullmatch(r'-[alntupsreio]+', words[1]):
        return words
    if name == 'ip' and len(words) > 2 and words[1] in {'address', 'addr', 'a', 'route', 'link'}:
        if words[2] in {'show', 'list'}:
            return words
    if name == 'systemctl' and len(words) > 1 and words[1] in {
            'status', 'show', 'is-active', 'is-enabled', 'list-units', 'list-unit-files', 'list-timers'}:
        if _read_options(words[2:], {'--no-pager', '--all', '-a', '--full', '-l', '--plain', '--quiet', '-q', '--failed'},
                         {'--type', '-t', '--state', '--property', '-p', '--lines', '-n'}, r'[A-Za-z0-9_.@:*?-]+'):
            return [words[0], '--no-pager', *words[1:]]
    if name == 'journalctl':
        if _read_options(words[1:], {'--no-pager', '--all', '-a', '--full', '-l', '--utc', '--reverse', '-r', '-k',
                                    '--dmesg', '--quiet', '-q', '--list-boots'},
                         {'--unit', '-u', '--lines', '-n', '--since', '--until', '--output', '-o', '--priority', '-p',
                          '--boot', '-b', '--identifier', '-t', '--grep', '-g'}, r'[A-Z_]+=[^\x00-\x1f]+'):
            return [words[0], '--no-pager', *words[1:]]
    if name == 'psql':
        return _psql(words)
    if name == 'docker' and len(words) > 1:
        action = words[1]
        if action in {'ps', 'images', 'version', 'info', 'inspect', 'logs'}:
            return words
        if action == 'stats' and '--no-stream' in words:
            return words
        if len(words) > 2 and action in {'container', 'image', 'volume', 'network'} and words[2] in {'ls', 'inspect'}:
            return words
        if action == 'exec' and len(words) > 3 and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', words[2]):
            inner = _read_command(words[3:])
            if inner and _base(words[3]) not in {'docker', 'systemctl', 'journalctl'}:
                return [*words[:3], *inner]
    return None


def assess(command):
    try:
        tokens = _tokens(command)
    except ValueError:
        return {'action': 'confirm', 'reason': '无法完整解析命令，需要确认后执行。'}
    if _destructive(tokens):
        return {'action': 'block', 'reason': '禁止删除、清空、格式化或卸载操作；本次命令不会执行。'}
    result = {'action': 'confirm', 'reason': '包含脚本、复杂语法或未识别操作，需要确认影响后执行。'}
    if not tokens or any(c in command for c in ('$', '`', '\\')):
        return result
    segments, current, joins = [], [], []
    for token in tokens:
        if token in {'&&', '|'}:
            if not current:
                return result
            segments.append(current)
            current = []
            joins.append(token)
        elif token and all(c in ';&|<>()\n' for c in token):
            return result
        else:
            current.append(token)
    if not current:
        return result
    segments.append(current)
    commands = [_read_command(words) for words in segments]
    if any(words is None for words in commands):
        return result
    normalized = shlex.join(commands[0])
    for join, words in zip(joins, commands[1:]):
        normalized += ' ' + join + ' ' + shlex.join(words)
    return {'action': 'auto', 'reason': '已识别为常见只读查询，可自动执行。', 'command': normalized}
