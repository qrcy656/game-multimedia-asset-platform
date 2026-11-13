# frozen_string_literal: true
# Invoked by GitLab's supported post-reconfigure hook. No credentials are logged.
require 'json'
require 'openssl'
require 'securerandom'
require 'fileutils'
require 'uri'

directory = '/var/opt/fantasy-bootstrap'
path = File.join(directory, 'oauth.json')
key = ENV.fetch('COMMUNITY_IDENTITY_SECRET')
raise 'Missing installation identity key' if key.length < 32
address = ENV.fetch('COMMUNITY_GITLAB_URL').sub(%r{/+$}, '')
origin = ENV.fetch('APP_ORIGIN').sub(%r{/+$}, '')
[address, origin].each do |value|
  parsed = URI.parse(value)
  raise 'Invalid public origin' unless %w[http https].include?(parsed.scheme) && parsed.host && !parsed.userinfo && !parsed.query && !parsed.fragment
end
callback = origin + '/auth/gitlab/callback'
FileUtils.mkdir_p(directory, mode: 02770)
File.umask(0027)

File.open(File.join(directory, 'bootstrap.lock'), File::RDWR | File::CREAT, 0640) do |lock|
  lock.flock(File::LOCK_EX)
  previous = nil
  if File.exist?(path)
    raise 'Invalid bootstrap file type' if File.symlink?(path) || File.size(path) > 32768
    envelope = JSON.parse(File.read(path))
    expected = OpenSSL::HMAC.hexdigest('SHA256', key, envelope.fetch('payload'))
    raise 'Installation key changed; restore its original key' unless OpenSSL.fixed_length_secure_compare(expected, envelope.fetch('signature'))
    previous = JSON.parse(envelope.fetch('payload'))
  end

  administrator = User.find_by(id: 1)
  # Once initialized, upstream administrators may change roles or suspend the
  # account. Reconfigure must preserve those decisions rather than restore them.
  raise 'GitLab installation administrator is unavailable' unless administrator && (previous || (administrator.admin? && administrator.active?))
  application_class = defined?(Authn::OauthApplication) ? Authn::OauthApplication : Doorkeeper::Application
  application = previous && application_class.find_by(uid: previous.fetch('client_id'))
  if previous
    raise 'Managed OAuth application was removed; restore or configure OAuth manually' unless application
    raise 'Managed OAuth secret was changed; configure OAuth manually' unless application.secret_matches?(previous.fetch('client_secret'))
    raise 'Managed OAuth application ownership changed' unless application.name == 'Fantasy Community (managed)' && application.owner_id.nil?
    application.update!(redirect_uri: callback, scopes: 'read_user', confidential: true, trusted: true)
    client_secret = previous.fetch('client_secret')
  else
    attributes = {name: 'Fantasy Community (managed)', redirect_uri: callback,
                  scopes: 'read_user', confidential: true, trusted: true, owner: nil}
    if application_class.reflect_on_association(:organization)
      attributes[:organization] = Organizations::Organization.default_organization
    end
    application = application_class.create!(attributes)
    client_secret = application.plaintext_secret
    raise 'OAuth application did not return its new secret' if client_secret.nil? || client_secret.empty?
  end

  # Keep the trusted numeric administrator identity and platform UUID. v2 makes
  # GitLab authoritative for this account; no username/password is changed.
  payload = JSON.generate({version: 2, url: address, redirect_uri: callback,
                           client_id: application.uid, client_secret: client_secret,
                           admin_user_id: administrator.id,
                           installation_id: previous ? previous.fetch('installation_id') : SecureRandom.uuid})
  envelope = JSON.generate({payload: payload, signature: OpenSSL::HMAC.hexdigest('SHA256', key, payload)})
  temporary = path + '.tmp-' + SecureRandom.hex(8)
  begin
    File.open(temporary, File::WRONLY | File::CREAT | File::EXCL, 0640) do |output|
      output.write(envelope)
      output.flush
      output.fsync
    end
    File.rename(temporary, path)
  ensure
    File.delete(temporary) if File.exist?(temporary)
  end
end
puts 'Fantasy Community managed OAuth initialization complete.'
