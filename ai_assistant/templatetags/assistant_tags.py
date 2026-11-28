from django import template

from ..models import AssistantConfig

register = template.Library()

# The chat button is for visitors. Administrator pages do not show it.
ADMIN_PREFIX = '/manage/'


@register.inclusion_tag('ai_assistant/_widget.html', takes_context=True)
def assistant_widget(context):
    request = context.get('request')
    config = AssistantConfig.load()
    on_admin_page = bool(request) and request.path.startswith(ADMIN_PREFIX)
    return {
        'enabled': config.enabled and not on_admin_page,
        'greeting': config.greeting,
        'csrf_token': context.get('csrf_token', ''),
    }
