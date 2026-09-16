k6 fixture corpus (PII-free, specs/09 §3.4).

- small.txt (~1KB plain text)
- medium.md (~50KB markdown)
- handbook.pdf (8MB, generated in CI — NOT committed; upload_burst.js falls
  back to the JSON /ingest path when it is absent so local runs stay green).

Generate the PDF fixture in CI with: python loadtests/k6/fixtures/make_pdf.py
