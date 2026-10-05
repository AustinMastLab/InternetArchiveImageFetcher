import json
import os
import tempfile

from lambda_function import _extract_ia_identifier_and_file


def run():
    samples = [
        "https://ia801809.us.archive.org/4/items/UK_Herbarium1/C%3A/Users/qtowe/Downloads/KY0002001_KY0005000/KY0004000.jpg",
        "https://archive.org/download/UK_Herbarium1/C%3A/Users/qtowe/Downloads/KY0002001_KY0005000/KY0004000.jpg",
    ]

    parsed = []
    for uri in samples:
        identifier, file_name = _extract_ia_identifier_and_file(uri)
        parsed.append({"uri": uri, "identifier": identifier, "file": file_name})

    with tempfile.TemporaryDirectory() as temp_dir:
        nested_file = os.path.join(temp_dir, parsed[0]["file"])
        os.makedirs(os.path.dirname(nested_file), exist_ok=True)
        with open(nested_file, "wb") as fh:
            fh.write(b"ok")

        parsed.append(
            {
                "nested_download_path": nested_file,
                "exists": os.path.exists(nested_file),
            }
        )

    print(json.dumps(parsed, indent=2))


if __name__ == "__main__":
    run()

