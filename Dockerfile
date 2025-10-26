FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY forge_review_app.py .

ENTRYPOINT ["python", "/app/forge_review_app.py"]


