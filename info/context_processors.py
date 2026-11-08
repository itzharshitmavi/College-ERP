from datetime import timezone as dt_timezone
from django.utils import timezone
from info.models import SpecialUpdateNote, ChatMessage, NoticeReadState, ChatReadState
from info.models import HODAssignment

def new_notices_flag(request):
    has_new_notices = False
    if request.user.is_authenticated and not request.user.is_superuser:
        latest_notice_id = SpecialUpdateNote.objects.order_by('-id').values_list('id', flat=True).first()
        if latest_notice_id:
            state, _ = NoticeReadState.objects.get_or_create(user=request.user)
            has_new_notices = int(latest_notice_id) > int(state.last_seen_notice_id)
    return {'has_new_notices': has_new_notices}


def new_messages_flag(request):
    has_new_messages = False
    if request.user.is_authenticated:
        latest_message_at = ChatMessage.objects.order_by('-created_at').values_list('created_at', flat=True).first()
        if latest_message_at:
            state, _ = ChatReadState.objects.get_or_create(user=request.user)
            if state.last_seen_message_at is None:
                # First-time viewer: show dot when chat has any message history.
                has_new_messages = True
            else:
                has_new_messages = latest_message_at > state.last_seen_message_at
    return {'has_new_messages': has_new_messages}


def theme_mode(request):
    mode = request.session.get('theme_mode', 'light')
    if mode not in {'light', 'dark'}:
        mode = 'light'
    return {'theme_mode': mode}


def can_manage_subjects(request):
    if not request.user.is_authenticated or not hasattr(request.user, 'teacher'):
        return {
            'can_manage_subjects': False,
            'hod_role_tags': [],
        }

    active_assignments = HODAssignment.objects.filter(
        teacher=request.user.teacher,
        is_active=True,
    ).select_related('department').order_by('year', 'department__name')

    hod_role_tags = [
        f"{assignment.department.name} - Year {assignment.year}"
        for assignment in active_assignments
    ]

    return {
        'can_manage_subjects': bool(hod_role_tags),
    }
