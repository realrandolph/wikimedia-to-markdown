
This repository contains a Python script that crawls a MediaWiki-style wiki and exports it into a Markdown corpus optimized for upload as Custom GPT Knowledge. The workflow uses `uv` to manage dependencies and execution inside an isolated virtual environment without manual venv setup.

Prerequisites are Python 3.10 or newer and `uv` installed on your system. You can install `uv` via your system package manager or from https://github.com/astral-sh/uv.

To run the exporter with `uv`, you do not need to create or activate a virtual environment yourself. `uv` will resolve dependencies, create an ephemeral environment, and execute the script in one step.

From the repository root, run:

```bash
uv run export_wiki.py https://ringofbrodgar.com
```

On first run, `uv` will download and cache the required dependencies (`requests`, `beautifulsoup4`) and then execute the script. Subsequent runs reuse the cached environment and are fast.

If you want the environment to be explicit and reusable across runs, initialize a project environment once:

```bash
uv venv
uv pip install requests beautifulsoup4
```

Then execute the script using:

```bash
uv run export_wiki.py https://ringofbrodgar.com
```

The exporter writes its output to a directory named `wiki_export` by default. Inside it you will find a `pages/` directory containing one Markdown file per wiki page and a `manifest.jsonl` file mapping titles, source URLs, and filenames. This structure is intended to upload directly as Knowledge files for a Custom GPT.

Common customizations are passed as flags. For example, to limit the crawl size or change the output directory:

```bash
uv run export_wiki.py https://ringofbrodgar.com -n 500 -o out_ring
```

The crawler respects `robots.txt` and crawl delays by default. Use the script responsibly and only against sites you are permitted to archive.

The exported Markdown files are intentionally clean, text-focused, and free of JavaScript or navigation chrome, which yields better retrieval quality when used as a Custom GPT knowledge base.


