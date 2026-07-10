FROM python:3.13-slim

# WeasyPrint (invoice PDFs) and pytesseract (OCR) need system libraries
# beyond what pip installs.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runtime data directories — mounted as volumes in docker-compose, created
# here too so the app has somewhere to write if run standalone.
RUN mkdir -p data/abrechnung output/invoices cache logs

EXPOSE 8000

CMD ["uvicorn", "backend:app", "--host", "0.0.0.0", "--port", "8000"]
