"""CoinWhisper: guarded migration of existing SMC targets to TP1=3R, TP2=4R.
Run from repository root: python apply_rr34.py
Fails without changing files if the expected code has changed.
"""
from pathlib import Path

REPLACEMENTS = {
    'strategy.py': [
        ('entry + 1.5 * risk, entry + 2.5 * risk', 'entry + 3.0 * risk, entry + 4.0 * risk'),
        ('entry - 1.5 * risk, entry - 2.5 * risk', 'entry - 3.0 * risk, entry - 4.0 * risk'),
        ('direction * 1.5 * risk', 'direction * 3.0 * risk'),
        ('direction * 2.5 * risk', 'direction * 4.0 * risk'),
    ],
    'bot.py': [
        ('(1.5R نسبت به ورود شرطی)', '(3R نسبت به ورود شرطی)'),
        ('(2.5R نسبت به ورود شرطی)', '(4R نسبت به ورود شرطی)'),
    ],
    'replay.py': [
        ('result, exit_at = 1.5 - cost_in_r, j', 'result, exit_at = 3.0 - cost_in_r, j'),
    ],
}


def prepare(root: Path) -> dict[Path, str]:
    proposed = {}
    for filename, rules in REPLACEMENTS.items():
        path = root / filename
        original = path.read_text(encoding='utf-8')
        updated = original
        for old, new in rules:
            count = updated.count(old)
            if count != 1:
                raise RuntimeError(f'{filename}: expected exactly one occurrence of {old!r}; found {count}. No files changed.')
            updated = updated.replace(old, new)
        compile(updated, filename, 'exec')
        proposed[path] = updated
    return proposed


def main() -> None:
    changes = prepare(Path('.'))
    for path, text in changes.items():
        path.write_text(text, encoding='utf-8')
        print(f'UPDATED {path}')
    print('RR34_PATCH_OK')


if __name__ == '__main__':
    main()
