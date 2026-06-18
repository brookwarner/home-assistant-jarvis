FROM python:3.12-alpine

# Build deps in case any wheel needs compiling; tzdata for local-time conversion;
# git so the recent_changes tool can read the deployed code / HA config history.
RUN apk add --no-cache gcc musl-dev libffi-dev tzdata git

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY run.sh /app/run.sh
RUN chmod +x /app/run.sh

CMD [ "/app/run.sh" ]
