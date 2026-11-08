from django import template

register = template.Library()

@register.filter
def get_item(dictionary, key):
    if dictionary and key is not None:
        return dictionary.get(key)
    return None
