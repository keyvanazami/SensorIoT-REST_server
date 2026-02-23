# syntax=docker/dockerfile:1
#FROM ubuntu:22.04
FROM nginx

# install app dependencies
RUN apt-get update && apt-get install -y python3 python3-pip python3-venv 
#RUN apt install -y certbot
#RUN apt-get remove -y certbot

RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt / 
RUN pip install -r requirements.txt


# Add your application code
COPY ./*.py /
COPY ./startup.sh /
COPY ./nginx.conf /etc/nginx/
RUN ln -s /app/venv/bin/certbot /usr/bin/certbot
COPY options-ssl-nginx.conf /etc/letsencrypt/options-ssl-nginx.conf 
COPY certs/*.pem /etc/letsencrypt/live/brintontech.com/
COPY ./ssl-dhparams.pem /etc/letsencrypt/ssl-dhparams.pem
#RUN certbot --nginx
COPY public /

# final configuration
#ENV FLASK_APP=hello
EXPOSE 443 
EXPOSE 80

CMD ["./startup.sh"]
