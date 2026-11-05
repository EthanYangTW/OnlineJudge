import hashlib
import ipaddress
import os

from django.core.cache import cache
from django.db import transaction

from .models import ClassroomAssistant, ClassroomQuiz, Enrollment


def is_classroom_owner(user, classroom):
    return bool(user.is_authenticated and not user.is_disabled and
                (classroom.teacher_id == user.id or user.is_super_admin()))


def is_classroom_teacher(user, classroom):
    return bool(is_classroom_owner(user, classroom) or
                (user.is_authenticated and not user.is_disabled and
                 ClassroomAssistant.objects.filter(classroom=classroom, user=user).exists()))


def client_ip(request):
    """Trust a forwarded address only when the immediate peer is a configured proxy."""
    peer = request.META.get("REMOTE_ADDR", "")
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return None
    cidrs = os.environ.get("FCU_TRUSTED_PROXY_CIDRS", "127.0.0.0/8,::1")
    trusted = False
    for raw in cidrs.split(","):
        try:
            if peer_ip in ipaddress.ip_network(raw.strip(), strict=False):
                trusted = True
                break
        except ValueError:
            continue
    candidate = request.META.get("HTTP_X_REAL_IP", "") if trusted else ""
    try:
        return str(ipaddress.ip_address(candidate or peer))
    except ValueError:
        return None


def login_failure_key(request, username):
    address = client_ip(request) or "unknown"
    digest = hashlib.sha256((address + ":" + username.casefold()).encode("utf-8")).hexdigest()
    return "fcu-login:" + digest


def login_is_limited(request, username):
    return cache.get(login_failure_key(request, username), 0) >= 5


def record_login_failure(request, username):
    key = login_failure_key(request, username)
    if not cache.add(key, 1, timeout=600):
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=600)


def clear_login_failures(request, username):
    cache.delete(login_failure_key(request, username))


def classroom_access(user, classroom, request_ip=None):
    if is_classroom_teacher(user, classroom):
        return True
    if not user.is_authenticated or user.is_disabled:
        return False
    with transaction.atomic():
        try:
            enrollment = Enrollment.objects.select_for_update().get(classroom=classroom, user=user)
        except Enrollment.DoesNotExist:
            return False
        if not classroom.ip_lock_enabled or enrollment.ip_lock_disabled:
            return True
        try:
            normalized_ip = str(ipaddress.ip_address(request_ip or ""))
        except ValueError:
            return False
        if enrollment.locked_ip and enrollment.locked_ip != normalized_ip:
            return False
        if not enrollment.locked_ip:
            enrollment.locked_ip = normalized_ip
            enrollment.save(update_fields=["locked_ip"])
        return True


def classroom_contest_access(user, contest, request_ip=None):
    quiz = ClassroomQuiz.objects.select_related("classroom").filter(contest=contest).first()
    return True if quiz is None else classroom_access(user, quiz.classroom, request_ip)
