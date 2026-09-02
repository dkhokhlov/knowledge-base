FROM python:3.13-slim-bullseye

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# markitdown + markitdown-ocr only. NO openai (the [llm] extra), NO [all]
# (no ffmpeg/exiftool/git — we do not handle audio/video/exif in v1).
# markitdown-ocr's core deps (pdfminer.six, pdfplumber, PyMuPDF, mammoth,
# python-docx, python-pptx, pandas, openpyxl, Pillow) are installed transitively.
# Pin both to the validated Stage B versions.
RUN pip --no-cache-dir install \
    markitdown==0.1.7 \
    markitdown-ocr==0.1.0

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin app

COPY --chown=1000:1000 oursvc.py /app/oursvc.py

USER 1000:1000
WORKDIR /app
EXPOSE 8080

ENTRYPOINT ["python", "/app/oursvc.py"]