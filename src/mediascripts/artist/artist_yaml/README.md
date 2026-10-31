# artist_yaml

Shared discovery, exclusions, backup cleanup, and validation helpers live in
[artist_yaml_common.py](./artist_yaml_common.py). The auto-populate script uses
these helpers rather than maintaining separate implementations.

## Database-backed artist workflows

`artist-yaml-reformat` and `artist-yaml-test` use artist names and directories
from the `artist` table in
`$MOONGAS_COLLECTION_ROOTDIR/mediascan.db`. Set that variable to the active
collection directory before running either command.

```bash
artist-yaml-reformat
artist-yaml-reformat --reformat-all
artist-yaml-test
artist-yaml-test --source-path /data/Music
```

The reformatter applies the built-in city/region corrections, or rewrites all
artist YAML files with `--reformat-all`. The validator checks each database
artist's `artist.yml` file and reports YAML files found under the source paths
that are not associated with a database artist. Repeat `--source-path` to scan
multiple media roots.

## artist_yaml_auto_populate.py

# Artist YAML LLM Auto-Populate Script (`artist_yaml_llm_auto_populate.py`)

This Python script automates the process of completing and validating missing fields in artist YAML configuration files (`artist.yml`) for a music collection using a Large Language Model (LLM).

---

## Key Features & Functionality

* **LLM-Powered Auto-Population:** Leverages an OpenAI-compatible API (such as GitHub Models or OpenAI) to fill in missing fields based on strict system instructions and reference examples.
* **Few-Shot Learning:** Uses completed YAML files from the `A` directory (`LETTER_DIRS_FOR_REFERENCE_EXAMPLES`) as schema and content references to guide the model for files in directories `B` through `Z`.
* **Safe File Handling:** 
  * Creates a `.bak` backup file alongside the original before overwriting.
  * Writes updates atomically using temporary files (`tempfile.NamedTemporaryFile`) to prevent data corruption.
* **Robust Validation:** Validates generated YAML syntax and schema requirements, handling validation warnings and API errors gracefully.

---

## Command-Line Options

| Option | Argument | Description |
| :--- | :--- | :--- |
| `--clean` | *None* | Deletes all `.bak` backup files in the collection root and exits immediately. |
| `--sleep` | `INT` | Specifies the number of seconds to pause between processing loops (default: `30`). |
| `--llm` | `true/false` | Enables or disables LLM lookups (default: `true`). Use `--llm=false` to check files without changing them. |
| `--file` | `PATH` | Process a single specific artist YAML file instead of scanning all directories. |
| `--force` | *None* | Force processing of all files, ignoring the pre-pass duplicate check and validation check. |
---

## Environment Variables

* `MOONGAS_COLLECTION_ROOTDIR`: Points to the root directory of the Moongas collection data (defaults to `../../moongas-collection-demo/data`).
* `LLM_BASE_URL` (or `GITHUB_MODELS_BASE_URL`): Configures the OpenAI-compatible API endpoint (defaults to `https://models.github.ai/inference`).
* `LLM_MODEL` (or `GITHUB_MODEL`): Specifies the model name (defaults to `openai/gpt-4o-mini`).
* `LLM_API_KEY` (or `GITHUB_TOKEN`, `OPENAI_API_KEY`): Authentication credential for the LLM provider.
* `LOG_LEVEL`: Adjusts logging verbosity (e.g., set to `DEBUG` for detailed diagnostic output).

---

## Example Usage

To run the script with a specific collection directory and clean up backups beforehand:

```bash
# Clean existing backups
python scripts/artist_yaml_llm_auto_populate.py --clean

# Run the auto-population loop with a custom root directory
MOONGAS_COLLECTION_ROOTDIR=/path/to/collection python scripts/artist_yaml_llm_auto_populate.py --sleep 10

# Populate specified artist YAML file
# Note: artist-yaml-auto-populate is a symlink created by mediascripts --create-script-symlinks
# in a media collection root dir, which is presumed to have MOONGAS_COLLECTION_ROOTDIR set
# (see script show-env)
./artist-yaml-auto-populate --file "/home/user/Downloads/data/Music/The Red Star Singers/artist.yml" --force
```

> **Note:** Ensure your API key (`GITHUB_TOKEN` or `LLM_API_KEY`) and dependencies (like `openai`, `tqdm`, and `dataclass_wizard`) are properly configured in your environment before executing.
