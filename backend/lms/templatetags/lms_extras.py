from django import template

register = template.Library()

@register.filter(name='dict_get')
def dict_get(value, arg):
    """Lấy giá trị từ dictionary bằng key động trong Django templates."""
    try:
        return value.get(arg)
    except (AttributeError, TypeError):
        return None
