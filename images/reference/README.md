# Character reference photo

Drop the recurring character's reference photo here as:

    images/reference/character.jpg   (or .png)

The generator (`tools/generate_concept_art.py`) reads the path from
`tools/concepts.json` -> `character.reference_image` and attaches it to every
image request so the model matches his likeness (long grey-white beard, bald
head, glasses). If the file is absent, the generator falls back to the text
description alone.
