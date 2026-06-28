FROM python:3.11-slim

# WeasyPrint needs Pango + Cairo. PyMuPDF ships its own MuPDF.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libharfbuzz0b \
    libpangoft2-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    shared-mime-info \
    fonts-liberation \
    fonts-dejavu-core \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p reports memory

EXPOSE 5000

CMD ["gunicorn", "--workers=1", "--threads=8", "--bind=0.0.0.0:5000", "--timeout=300", "app:app"]
