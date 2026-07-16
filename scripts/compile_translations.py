from pathlib import Path

import polib


def main() -> None:
    locale_root = Path(__file__).resolve().parent.parent / "locale"
    catalogs = sorted(locale_root.glob("*/LC_MESSAGES/django.po"))
    if not catalogs:
        raise SystemExit("No translation catalogs found")

    for catalog in catalogs:
        output = catalog.with_suffix(".mo")
        polib.pofile(catalog).save_as_mofile(output)
        print(f"Compiled {catalog.relative_to(locale_root.parent)}")


if __name__ == "__main__":
    main()
