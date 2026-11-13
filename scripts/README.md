# Scripts

Give each script a descriptive filename that identifies its purpose without
requiring context. Include the subject and action in the name.

## Launch assets

Regenerate the standalone interactive demo with
`python3 scripts/render_sqlassert_interactive_demo.py docs/assets`.

Regenerate the GitHub social preview with
`python3 scripts/render_sqlassert_social_preview.py docs/assets`.
The preview renderer requires Pillow and the macOS system fonts. Both scripts
use the repository's `.venv/bin/sqlassert`.
