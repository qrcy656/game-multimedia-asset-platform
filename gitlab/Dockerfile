ARG GITLAB_IMAGE=gitlab/gitlab-ce:19.4.7-ce.0
FROM ${GITLAB_IMAGE}
COPY theme.js install-theme.rb install-oauth-redirect.rb /opt/fantasy-theme/
RUN /opt/gitlab/embedded/bin/ruby /opt/fantasy-theme/install-theme.rb && \
    /opt/gitlab/embedded/bin/ruby /opt/fantasy-theme/install-oauth-redirect.rb
