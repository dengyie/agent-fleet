"""Owner-scoped model catalog to signed native-session configuration.

Keys are resolved only for an authenticated machine's live signed command.
Assistant results, command envelopes and audits never contain credentials.
"""
from fleet_core.session_models import AGENT_PROTOCOLS, validate_model_delivery
from hub.application.errors import ApplicationError
from hub.infrastructure.platform_db_catalog import catalog_digest


def _error(code, status=409):
    raise ApplicationError(code, code, status)


class SessionModelService:
    def __init__(self, repository, secrets, sessions, supervisor, authorize=None):
        self.repository, self.secrets = repository, secrets
        self.sessions, self.supervisor, self.authorize = sessions, supervisor, authorize

    @staticmethod
    def _family(session):
        family = session.get('agent_family')
        return 'claude_code' if family == 'claude' else family

    @staticmethod
    def _endpoint(model, family):
        if not model or not model['enabled']:
            _error('model_unavailable')
        endpoint = model['provider_config'].get('agent_endpoints', {}).get(AGENT_PROTOCOLS.get(family))
        if model['provider'] not in {'openai_compatible', 'openai-compatible'} or not endpoint:
            _error('model_protocol_unavailable')
        if not model.get('secret_ref'):
            _error('model_credential_unavailable')
        return endpoint

    def list_models(self, owner, session):
        family = self._family(session)
        models = []
        for row in self.repository.list_models(owner):
            try:
                endpoint = self._endpoint(row, family)
            except ApplicationError:
                continue
            models.append({'profile_id': row['profile_id'], 'model': row['model'],
                           'base_url': endpoint, 'protocol': AGENT_PROTOCOLS[family],
                           'digest': catalog_digest(row)})
        return {'models': models, 'available': 'configure_model' in session['allowed_actions'],
                'scope': 'next_native_resume', 'family': family,
                'configuration': 'settings.models.provider_config.agent_endpoints'}

    def payload(self, owner, session, args):
        mode = args.get('mode')
        if mode == 'original' and 'profile_id' not in args:
            return {'mode': mode, 'owner_id': owner}
        from platform_schema import validate_id, PlatformValidationError
        try:
            profile_id = validate_id(args.get('profile_id'), 'profile_id')
        except PlatformValidationError:
            _error('invalid_arguments', 400)
        if mode != 'catalog':
            _error('invalid_arguments', 400)
        model = self.repository.get_model(owner, profile_id)
        self._endpoint(model, self._family(session))
        return {'mode': mode, 'owner_id': owner, 'profile_id': profile_id, 'digest': catalog_digest(model)}

    def resolve(self, machine, command_id):
        command = self.supervisor.model_command(machine, command_id)
        payload = command['payload']
        owner = payload['owner_id']
        if self.authorize is not None and not self.authorize(owner):
            _error('forbidden', 403)
        session = self.sessions.get_session(command['target']['session_id'])['session']
        if (session['machine_id'] != machine or session.get('attempt_id') != command['target'].get('attempt_id')
                or 'configure_model' not in session['allowed_actions']):
            _error('session_action_unavailable')
        if payload['mode'] == 'original':
            return {'mode': 'original'}
        family = self._family(session)
        model = self.repository.get_model(owner, payload['profile_id'])
        endpoint = self._endpoint(model, family)
        if catalog_digest(model) != payload['digest']:
            _error('model_configuration_changed')
        try:
            key = self.secrets.resolve(model['secret_ref'])
        except Exception:
            _error('model_credential_unavailable')
        try:
            return validate_model_delivery({'mode': 'catalog', 'family': family,
                'profile_id': model['profile_id'], 'digest': payload['digest'], 'model': model['model'],
                'base_url': endpoint, 'api_key': key, 'headers': model['provider_config'].get('headers', {})}, family)
        except ValueError:
            _error('model_configuration_invalid')
