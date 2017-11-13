FROM python:3.6

RUN apt-get update
RUN apt-get install -y --no-install-recommends \
    gcc g++ build-essential libsnappy-dev

RUN pip install --upgrade awscli

WORKDIR /opt/cerebro-deep-rec-engine

ADD requirements.txt .
RUN pip install -r requirements.txt
RUN pip install tensorflow==1.3.0
RUN apt-get purge -y --auto-remove gcc g++ build-essential

ADD tiefrex /opt/cerebro-deep-rec-engine/tiefrex
ENV PYTHONPATH=$PYTHONPATH:/opt/cerebro-deep-rec-engine/tiefrex
RUN mkdir /tmp/tensorboard-logs
ADD bin/run.sh bin/run.sh

ENTRYPOINT ["bin/run.sh"]
