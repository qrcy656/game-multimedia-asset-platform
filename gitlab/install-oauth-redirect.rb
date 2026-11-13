# frozen_string_literal: true
# Preserve GitLab's pending OAuth authorization after a first administrator login.
# GitLab 19.4's empty-instance onboarding otherwise overwrites the OAuth return URL.
# This adapter changes no authentication, group, user, or application permissions.
root = '/opt/gitlab/embedded/service/gitlab-rails'
path = File.join(root, 'app/controllers/sessions_controller.rb')
source = File.read(path)
version = File.read(File.join(root, 'VERSION')).strip
raise 'Unsupported GitLab release for OAuth redirect adapter' unless version.match?(/\A19\.4\.\d+(?:-[a-zA-Z0-9.]+)?\z/)
raise 'OAuth redirect adapter was already installed' if source.include?('fantasy_oauth_return')

original = <<~'RUBY'
  def should_redirect_to_sm_onboarding?(resource)
    !Gitlab::CurrentSettings.gitlab_dedicated_instance? &&
      resource.can_admin_all_resources? &&
      !Group.exists?
  end
RUBY
original = original.lines.map { |line| '  ' + line }.join
raise 'GitLab OAuth onboarding contract changed; review adapter before upgrading' unless source.scan(original).length == 1

replacement = <<~'RUBY'
  def should_redirect_to_sm_onboarding?(resource)
    # Keep the same-origin authorization request saved by Doorkeeper. Ordinary
    # GitLab sign-ins retain GitLab's existing first-group onboarding behavior.
    fantasy_oauth_return = session[:user_return_to]
    return false if fantasy_oauth_return.is_a?(String) && fantasy_oauth_return.start_with?('/oauth/authorize?')

    !Gitlab::CurrentSettings.gitlab_dedicated_instance? &&
      resource.can_admin_all_resources? &&
      !Group.exists?
  end
RUBY
replacement = replacement.lines.map { |line| '  ' + line }.join
File.write(path, source.sub(original, replacement))

# Exercise the actual injected method with controlled session/instance state.
# No Rails initialization, database connection, or container startup is needed.
module Gitlab
  class CurrentSettings
    def self.gitlab_dedicated_instance? = false
  end
end
class Group
  def self.exists? = false
end
fixture = Class.new do
  attr_accessor :session
end
fixture.class_eval(replacement)
subject = fixture.new
administrator = Struct.new(:can_admin_all_resources?).new(true)
member = Struct.new(:can_admin_all_resources?).new(false)
{
  '/oauth/authorize?client_id=fixture&response_type=code' => false,
  '/oauth/authorize' => true,
  '/oauth/authorize/other?client_id=fixture' => true,
  'https://other.example/oauth/authorize?client_id=fixture' => true,
  '//other.example/oauth/authorize?client_id=fixture' => true,
  '/dashboard/projects' => true,
  nil => true,
  [] => true
}.each do |target, expected|
  subject.session = {user_return_to: target}
  raise 'OAuth redirect adapter behavior check failed' unless subject.should_redirect_to_sm_onboarding?(administrator) == expected
end
subject.session = {}
raise 'Ordinary member onboarding changed' if subject.should_redirect_to_sm_onboarding?(member)
puts 'Fantasy GitLab OAuth return adapter verified for GitLab ' + version
