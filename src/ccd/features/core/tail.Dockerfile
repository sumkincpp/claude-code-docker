WORKDIR /app

USER root
# Entrypoint to initialize /app environment
COPY core/ccd-entrypoint /usr/local/bin/ccd-entrypoint
RUN chmod +x /usr/local/bin/ccd-entrypoint
USER user

ENTRYPOINT ["/usr/local/bin/ccd-entrypoint"]
CMD ["bash"]
