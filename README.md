# font-forager

Download the fonts a web page loads, then generate a specimen and a drop-in
stylesheet.

font-forager fetches a page and its linked stylesheets, finds every font URL in
`@font-face` rules and `<link rel="preload" as="font">` tags, and downloads each
font into `data/<host>/`. It then:

- **Deduplicates** byte-identical files (the same font served under several
  `@font-face` family aliases).
- **Renames** each file from its own metadata — `<family-slug>-<weight>[-italic].<ext>`,
  with family taken from the name table and weight from `OS/2.usWeightClass` — so
  the filename reflects the font's real weight rather than any remapped
  `@font-face` `font-weight`.
- Writes a **`specimen.html`** showing, per font, the characters it actually
  contains (read from its cmap).
- Writes a **`style.css`** with one reusable `@font-face` rule per font, keyed on
  the font's real family and weight, using relative paths — so the folder can be
  dropped into a website as-is.

## Requirements

- Python 3.8+
- Dependencies (installed via `make setup`): [`fonttools`](https://github.com/fonttools/fonttools)
  and `brotli` (for reading `woff2`).

## Setup

```sh
make setup
```

This creates a `.venv` and installs the dependencies from `requirements.txt`.

## Usage

```sh
make run URL=https://example.com
```

Output is written to `data/<host>/`, containing the downloaded font files,
`specimen.html`, and `style.css`.

You can also run the script directly:

```sh
.venv/bin/python font-forager.py https://example.com
```

## Make targets

| Target            | Description                                            |
| ----------------- | ------------------------------------------------------ |
| `make setup`      | Create the venv and install dependencies               |
| `make run URL=…`  | Download the fonts a page loads + build `specimen.html` |
| `make clean`      | Remove the venv                                        |
| `make clean-data` | Remove the `data/` output directory                    |

## Limitations

This is a static fetch. Fonts injected at runtime by JavaScript are not
discovered, since the tool only reads the page's initial HTML and its linked
stylesheets.

## Legal

Downloading fonts from a website does not grant you a license to use them. Check
each font's license before reusing it in your own project.

## License

font-forager itself is released under the [MIT License](LICENSE).
