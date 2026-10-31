# Testing IcyLoom

Thanks for testing. This page says exactly what to check and how to report.

## What to check (5 minutes)

Install the wheel, then run the four commands below **in this order**.

```bash
python3 -m venv venv && source venv/bin/activate      # Windows: .\venv\Scripts\Activate.ps1
python -m pip install https://github.com/MlkBlk-IT/icy-loom/releases/download/v1.0.0/icyloom-1.0.0-py3-none-any.whl

icyloom init      # writes the configuration
icyloom doctor    # must end with "OK — aucune anomalie détectée." and exit 0
icyloom serve     # leave it running in a second terminal
icyloom check --transport all   # must print "21/21 vérifications réussies"
```

| Check | Expected result |
|---|---|
| `icyloom init` | `configuration écrite : …` |
| `icyloom doctor` | exit code **0**, no `PROBLÈMES` line |
| `icyloom serve` | four services on 8150 / 8151 / 8152 / 8153 |
| `icyloom check --transport all` | `21/21 vérifications réussies` |
| `icyloom paths` | the database and the logs are **not** inside `site-packages` |
| `icyloom client --target cursor --install` | the client config file is written |

On Windows, the database must land in `%LOCALAPPDATA%\icyloom`, on Linux in
`$XDG_DATA_HOME/icyloom` or `~/.local/share/icyloom`.

## What is expected to be noisy (not a bug)

- `doctor` may print **AVERTISSEMENTS** (no upstream configured, a tool
  executable missing, a port already used). Warnings do not fail the check.
- `tiktoken` may need a network access the first time to download its
  encoding table. Without it, IcyLoom silently falls back to a local
  approximation of the token count.

## How to report

Open an issue: https://github.com/MlkBlk-IT/icy-loom/issues

Use the **bug** template and paste:

1. your OS and your Python version (`python -V`);
2. the exact command and its full output;
3. `icyloom paths` and `icyloom doctor` output;
4. the relevant part of the log file (`icyloom paths` shows where the logs
   are).

Never paste a token, an API key or a private path you do not want public.

The **feedback** template is for anything that is not a crash: ergonomics,
missing features, documentation that confused you.

## Running the test suite yourself

```bash
python -m pip install -e ".[dev]"
python -m pytest -o addopts="" -q
```

The suite starts real servers on the ports 8154-8157 and 18150-18153 and uses
a temporary database: your own data is never touched.
