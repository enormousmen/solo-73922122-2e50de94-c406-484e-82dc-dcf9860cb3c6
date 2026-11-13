FROM alpine:3.20

# 本地演示用 SFTP 源：模拟仪器侧的 SFTP 服务器
RUN apk add --no-cache openssh-server \
    && adduser -D -u 1000 -h /home/inst instrument \
    && echo 'instrument:instrument' | chpasswd \
    && mkdir -p /home/inst/drop /home/inst/claim \
    && chown instrument:instrument /home/inst/drop /home/inst/claim \
    && chown root:root /home/inst && chmod 755 /home/inst

COPY sshd_config /etc/ssh/sshd_config
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 22
ENTRYPOINT ["/entrypoint.sh"]
