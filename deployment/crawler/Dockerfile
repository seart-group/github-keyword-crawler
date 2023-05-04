FROM python:3.10-alpine
LABEL maintainer="Ozren Dabić (dabico@usi.ch)"

ENV TMPDIR=/tmp

WORKDIR /crawler
COPY . ./

RUN pip install -r requirements.txt

ENTRYPOINT ["python3", "./main.py"]
