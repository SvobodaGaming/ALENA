FROM python:3.14-slim

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

# LibreOffice приводит DOCX, ODT и DOC к PDF – дальше все форматы проверяются
# одним и тем же кодом.
#
# Настоящие шрифты Microsoft обязательны, а не желательны: без них LibreOffice
# подставляет метрический клон Liberation Serif, и в готовом PDF гарнитура
# называется уже им. Вёрстка от подстановки не съезжает, но критерий «Times New
# Roman» проваливала бы каждая работа в DOCX. Пакет лежит в contrib и тянет
# файлы со стороннего сервера: в сети без внешнего доступа сборка встанет
# здесь – тогда шрифты ставят из заранее скачанного .deb.
RUN . /etc/os-release \
    && echo "deb http://deb.debian.org/debian ${VERSION_CODENAME} contrib" \
       > /etc/apt/sources.list.d/contrib.list \
    && apt-get update \
    && echo ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula \
       select true | debconf-set-selections \
    && apt-get install -y --no-install-recommends \
       libreoffice-writer \
       ttf-mscorefonts-installer \
       fontconfig \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p reports memory

EXPOSE 5000

CMD ["gunicorn", "--workers=1", "--threads=8", "--bind=0.0.0.0:5000", "--timeout=300", "app:app"]
