# A small, version-checked integration with GitLab's own two application stylesheets.
require 'json'
root = '/opt/gitlab/embedded/service/gitlab-rails/app/views/layouts'
head = File.join(root, '_head.html.haml')
source = File.read(head)
raise 'GitLab theme adapter was already installed' if source.include?('layouts/fantasy_theme')
start = source.index("  - if user_application_dark_mode?\n")
finish = source.index("  = universal_stylesheet_link_tag 'tailwind'", start || 0)
raise 'Unsupported GitLab layout: theme stylesheet block not found' unless start && finish
original = source[start...finish]
raise 'Unsupported GitLab layout: native color mode stylesheet contracts changed' unless original.include?('application_dark') && original.include?('user_application_system_mode?')
replacement = <<~'HAML'
  %meta{ name: 'color-scheme', content: 'light dark' }
  = universal_stylesheet_link_tag 'application', id: 'fantasy-gitlab-light', media: 'not all'
  = universal_stylesheet_link_tag 'application_dark', id: 'fantasy-gitlab-dark', media: 'not all'
  = yield :page_specific_styles
  = render 'layouts/fantasy_theme'

HAML
source[start...finish] = replacement.lines.map { |line| '  ' + line }.join
source = source.sub("  = render 'layouts/application_color_mode_js'\n", '')
File.write(head, source)
javascript = File.read('/opt/fantasy-theme/theme.js')
config = <<~'HAML'
  - fantasy_theme_config = { userId: current_user&.id, signedIn: !!current_user, currentMode: Gitlab::ColorModes.for_user(current_user).id, preferencesPath: profile_preferences_path }.to_json
  = javascript_tag "window.FantasyGitLabThemeConfig = #{json_escape(fantasy_theme_config)};".html_safe, nonce: content_security_policy_nonce
  = javascript_tag nonce: content_security_policy_nonce do
    :plain
HAML
File.write(File.join(root, '_fantasy_theme.html.haml'), config + javascript.lines.map { |line| '    ' + line }.join)
