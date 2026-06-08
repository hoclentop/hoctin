from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.http import JsonResponse, Http404
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Max, F, Q
from django.core.files.storage import default_storage
from django.conf import settings
from django.utils import timezone
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import login as auth_login
from .models import (
    Course, Test, DynamicTest, CourseBundle, TestBundle, 
    CourseOwnership, TestOwnership, Lesson, LessonProgress, MultiExerciseProgress,
    WalletTransaction, Profile, Attempt, SharedInstruction, TestPartInstruction, TestQuestion,
    AttemptAnswer, Choice, Question, QuestionGroup, TestRegulation, EquivalentQuestionGroup,
    BankAccount
)
from .services import ScoringService, JudgeSyncService
import random
import time
from functools import wraps
from django.core.cache import cache

def int_to_roman(n):
    romans = {1: 'I', 2: 'II', 3: 'III', 4: 'IV', 5: 'V', 6: 'VI', 7: 'VII', 8: 'VIII', 9: 'IX', 10: 'X'}
    return romans.get(n, str(n))

def roman_to_int(s):
    romans = {'I': 1, 'II': 2, 'III': 3, 'IV': 4, 'V': 5, 'VI': 6, 'VII': 7, 'VIII': 8, 'IX': 9, 'X': 10}
    return romans.get(s.upper(), 1)

def register(request):
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            # Tự động đảm bảo Profile được tạo cho user mới (được xử lý bởi signal)
            Profile.objects.get_or_create(user=user)
            auth_login(request, user)
            messages.success(request, f"Chào mừng {user.username}! Bạn đã đăng ký thành công.")
            return redirect('course_list')
    else:
        form = UserCreationForm()
    return render(request, 'registration/register.html', {'form': form})

def course_list(request):
    import datetime
    from django.utils import timezone
    
    q = request.GET.get('q', '').strip()
    
    # 1. Tìm kiếm kép nếu có tham số 'q'
    search_courses = None
    search_tests = None
    if q:
        search_courses = Course.objects.filter(
            Q(title__icontains=q) | Q(description__icontains=q)
        ).distinct()
        search_tests = Test.objects.filter(
            test_type='STANDALONE'
        ).filter(
            Q(title__icontains=q)
        ).distinct()
        
    # 2. Lấy dữ liệu mặc định (ngẫu nhiên)
    random_courses = Course.objects.order_by('?')[:3]
    random_tests = Test.objects.filter(test_type='STANDALONE').order_by('?')[:3]
    
    # 3. Lấy Nhật ký Hoạt động học tập thời gian thực
    # Lấy 30 tiến độ hoàn thành bài học gần nhất để lọc bỏ học thử của GV/admin
    recent_progress_raw = LessonProgress.objects.filter(
        is_completed=True
    ).select_related('user', 'lesson', 'lesson__course').order_by('-completed_at')[:30]
    
    # Lấy 30 lượt làm bài thi gần nhất để lọc bỏ làm thử của GV/admin
    recent_attempts_raw = Attempt.objects.select_related('user', 'test', 'test__creator').order_by('-start_time')[:30]
    
    activities = []
    for p in recent_progress_raw:
        # Lọc bỏ học thử của giáo viên tạo bài hoặc admin
        is_trial = (
            p.user.is_superuser or 
            p.user.is_staff or 
            (p.lesson.course.creator == p.user)
        )
        if not is_trial:
            activities.append({
                'user': p.user,
                'type': 'lesson',
                'lesson': p.lesson,
                'course': p.lesson.course,
                'timestamp': p.completed_at or timezone.now(),
            })
        
    for a in recent_attempts_raw:
        # Lọc bỏ làm thử đề thi của giáo viên tạo đề/khóa học hoặc admin
        is_trial = (
            a.user.is_superuser or 
            a.user.is_staff or 
            (a.test.creator == a.user)
        )
        if not is_trial:
            # Check if user is the creator of any course linked to this test
            from .models import Lesson
            if Lesson.objects.filter(test=a.test, course__creator=a.user).exists():
                is_trial = True
                
        if not is_trial:
            activities.append({
                'user': a.user,
                'type': 'attempt',
                'test': a.test,
                'score': a.total_score,
                'is_completed': a.end_time is not None,
                'timestamp': a.start_time,
            })
        
    # Gộp và sắp xếp theo timestamp giảm dần (mới nhất lên đầu)
    activities.sort(key=lambda x: x['timestamp'] if isinstance(x['timestamp'], datetime.datetime) else timezone.now(), reverse=True)
    activities = activities[:6]
    
    return render(request, 'lms/course_list.html', {
        'q': q,
        'search_courses': search_courses,
        'search_tests': search_tests,
        'random_courses': random_courses,
        'random_tests': random_tests,
        'activities': activities,
    })

def all_courses(request):
    courses = Course.objects.all().order_by('-id')
    return render(request, 'lms/all_courses.html', {
        'courses': courses,
    })

def course_detail(request, course_id):
    course = get_object_or_404(Course, id=course_id)
    is_owned = False
    first_lesson_id = None
    has_pending_transaction = False
    ownership = None
    if request.user.is_authenticated:
        ownership = CourseOwnership.objects.filter(user=request.user, course=course).first()
        is_owned = CourseOwnership.has_active_ownership(request.user, course)
        if is_owned:
            completed_ids = LessonProgress.objects.filter(user=request.user, is_completed=True).values_list('lesson_id', flat=True)
            first_incomplete = course.lessons.exclude(id__in=completed_ids).order_by('order_index').first()
            if first_incomplete:
                first_lesson_id = first_incomplete.id
            else:
                first_l = course.lessons.order_by('order_index').first()
                if first_l:
                    first_lesson_id = first_l.id
        else:
            first_l = course.lessons.order_by('order_index').first()
            if first_l:
                first_lesson_id = first_l.id
                
            has_pending_transaction = WalletTransaction.objects.filter(
                user=request.user,
                course=course,
                status='PENDING'
            ).exists()
    else:
        first_l = course.lessons.order_by('order_index').first()
        if first_l:
            first_lesson_id = first_l.id
            
    return render(request, 'lms/course_detail.html', {
        'course': course,
        'is_owned': is_owned,
        'first_lesson_id': first_lesson_id,
        'has_pending_transaction': has_pending_transaction,
        'ownership': ownership,
    })

def test_list(request):
    if request.user.is_authenticated and (
        request.user.is_superuser 
        or request.user.is_staff 
        or (hasattr(request.user, 'profile') and (request.user.profile.can_create_exams or request.user.profile.can_create_courses))
    ):
        tests = Test.objects.all().order_by('-id')
    else:
        tests = Test.objects.filter(test_type='STANDALONE').order_by('-id')
    return render(request, 'lms/test_list.html', {'tests': tests})

def test_detail(request, test_id):
    test = get_object_or_404(Test, id=test_id)
    
    is_registered = False
    registration = None
    if request.user.is_authenticated:
        # Bỏ qua yêu cầu đăng ký thi nếu user là admin, staff, hoặc người tạo đề thi / người tạo khóa học liên kết hoặc giáo viên
        is_bypass_test = (
            request.user.is_superuser 
            or request.user.is_staff 
            or test.creator == request.user
            or (hasattr(request.user, 'profile') and (request.user.profile.can_create_exams or request.user.profile.can_create_courses))
        )
        if not is_bypass_test:
            from lms.models import Lesson, ClassroomMembership
            # Check if accessing in the context of a classroom lesson via lesson_id
            lesson_id = request.GET.get('lesson_id') or request.POST.get('lesson_id')
            if lesson_id:
                lesson_context = Lesson.objects.filter(id=lesson_id, test=test).first()
                if lesson_context and ClassroomMembership.objects.filter(
                    student=request.user,
                    status='APPROVED',
                    classroom__items__lesson=lesson_context
                ).exists():
                    is_bypass_test = True

            if not is_bypass_test:
                associated_lessons = Lesson.objects.filter(test=test)
                for al in associated_lessons:
                    if al.course.creator == request.user or CourseOwnership.has_active_ownership(request.user, al.course):
                        is_bypass_test = True
                        break

                
        if is_bypass_test:
            is_registered = True
        else:
            registration = TestOwnership.objects.filter(user=request.user, test=test).first()
            if registration and registration.agreed_rules:
                is_registered = True
            
    is_upcoming = False
    is_ongoing = False
    is_finished = False
    now = timezone.now()
    
    if test.is_official and test.start_time:
        if now < test.start_time:
            is_upcoming = True
        elif test.end_time and now > test.end_time:
            is_finished = True
        else:
            is_ongoing = True
            
    has_pending_transaction = False
    if request.user.is_authenticated and not is_registered:
        has_pending_transaction = WalletTransaction.objects.filter(
            user=request.user,
            test=test,
            status='PENDING'
        ).exists()
            
    return render(request, 'lms/test_detail.html', {
        'test': test,
        'is_registered': is_registered,
        'registration': registration,
        'is_upcoming': is_upcoming,
        'is_ongoing': is_ongoing,
        'is_finished': is_finished,
        'now': now,
        'has_pending_transaction': has_pending_transaction,
    })

from django import forms

class CourseForm(forms.ModelForm):
    class Meta:
        model = Course
        fields = ['title', 'description', 'price', 'duration_days', 'learning_mode', 'thumbnail']
        labels = {
            'duration_days': 'Thời hạn học (ngày)',
        }
        help_texts = {
            'duration_days': 'Số ngày được học kể từ lúc mua/duyệt. Nhập 0 hoặc để trống nếu muốn học trọn đời.',
        }
        widgets = {
            'title': forms.TextInput(attrs={'class': 'w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 font-medium'}),
            'description': forms.Textarea(attrs={'rows': 5, 'class': 'w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500'}),
            'price': forms.NumberInput(attrs={'class': 'w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500'}),
            'duration_days': forms.NumberInput(attrs={'class': 'w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500'}),
            'learning_mode': forms.Select(attrs={'class': 'w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500'}),
            'thumbnail': forms.FileInput(attrs={'class': 'w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500'}),
        }

class LessonForm(forms.ModelForm):
    class Meta:
        model = Lesson
        fields = ['title', 'lesson_type', 'order_index', 'content', 'video_url', 'external_judge_link', 'external_problem_code', 'test']
        widgets = {
            'title': forms.TextInput(attrs={'class': 'w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500'}),
            'lesson_type': forms.Select(attrs={'class': 'w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500'}),
            'order_index': forms.NumberInput(attrs={'class': 'w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500'}),
            'content': forms.Textarea(attrs={'rows': 10, 'class': 'w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500'}),
            'video_url': forms.URLInput(attrs={'class': 'w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500'}),
            'external_judge_link': forms.URLInput(attrs={'class': 'w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500'}),
            'external_problem_code': forms.TextInput(attrs={'class': 'w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500'}),
            'test': forms.Select(attrs={'class': 'w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500'}),
        }

class UserEditForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email']
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 font-medium'}),
            'last_name': forms.TextInput(attrs={'class': 'w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 font-medium'}),
            'email': forms.EmailInput(attrs={'class': 'w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 font-medium'}),
        }

class ProfileEditForm(forms.ModelForm):
    class Meta:
        model = Profile
        fields = ['vnoj_username', 'codeforces_username', 'dmoj_username']
        labels = {
            'dmoj_username': 'Username on.hsgtin.vn',
        }
        widgets = {
            'vnoj_username': forms.TextInput(attrs={'class': 'w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 font-medium'}),
            'codeforces_username': forms.TextInput(attrs={'class': 'w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 font-medium'}),
            'dmoj_username': forms.TextInput(attrs={'class': 'w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 font-medium'}),
        }

from django.contrib.auth.forms import PasswordChangeForm

class StyledPasswordChangeForm(PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({
                'class': 'w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 font-medium'
            })

def teacher_required(view_func):
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not request.user.is_superuser and not getattr(request.user.profile, 'can_create_courses', False):
            messages.error(request, "Bạn không có quyền truy cập chức năng này.")
            return redirect('course_list')
        return view_func(request, *args, **kwargs)
    return _wrapped_view

@teacher_required
def create_course(request):
    if request.method == 'POST':
        form = CourseForm(request.POST, request.FILES)
        if form.is_valid():
            course = form.save(commit=False)
            course.creator = request.user
            course.save()
            messages.success(request, f"Đã tạo khóa học '{course.title}' thành công.")
            return redirect('course_detail', course_id=course.id)
    else:
        form = CourseForm()
    return render(request, 'lms/course_form.html', {'form': form, 'title': 'Tạo khóa học mới'})

@teacher_required
def edit_course(request, course_id):
    course = get_object_or_404(Course, id=course_id)
    if not request.user.is_superuser and course.creator != request.user:
        messages.error(request, "Bạn không phải người tạo khóa học này.")
        return redirect('course_list')
        
    if request.method == 'POST':
        form = CourseForm(request.POST, request.FILES, instance=course)
        if form.is_valid():
            form.save()
            messages.success(request, f"Đã cập nhật thông tin khóa học '{course.title}' thành công.")
            return redirect('course_detail', course_id=course.id)
    else:
        form = CourseForm(instance=course)
    return render(request, 'lms/course_form.html', {'form': form, 'course': course, 'title': f"Sửa khóa học: {course.title}"})

@teacher_required
def delete_course(request, course_id):
    course = get_object_or_404(Course, id=course_id)
    if not request.user.is_superuser and course.creator != request.user:
        messages.error(request, "Bạn không phải người tạo khóa học này.")
        return redirect('course_list')
        
    if request.method == 'POST':
        course.delete()
        messages.success(request, "Đã xóa khóa học thành công.")
        return redirect('course_list')
    return render(request, 'lms/confirm_delete.html', {'object': course, 'cancel_url': 'course_detail', 'cancel_args': [course.id]})

@teacher_required
def manage_course_structure(request, course_id):
    course = get_object_or_404(Course, id=course_id)
    if not request.user.is_superuser and course.creator != request.user:
        messages.error(request, "Bạn không có quyền quản lý khóa học này.")
        return redirect('course_list')
    lessons = course.lessons.all().order_by('order_index')
    return render(request, 'lms/manage_course_structure.html', {'course': course, 'lessons': lessons})

@teacher_required
def create_lesson(request, course_id):
    course = get_object_or_404(Course, id=course_id)
    if not request.user.is_superuser and course.creator != request.user:
        messages.error(request, "Bạn không có quyền quản lý khóa học này.")
        return redirect('course_list')
        
    if request.method == 'POST':
        form = LessonForm(request.POST)
        if form.is_valid():
            lesson = form.save(commit=False)
            lesson.course = course
            lesson.save()
            messages.success(request, f"Đã thêm bài học '{lesson.title}' thành công.")
            return redirect('manage_course_structure', course_id=course.id)
    else:
        # Tự động lấy order_index tiếp theo
        next_order = (course.lessons.aggregate(Max('order_index'))['order_index__max'] or 0) + 1
        form = LessonForm(initial={'order_index': next_order})
        
    return render(request, 'lms/lesson_form.html', {'form': form, 'course': course, 'title': 'Thêm bài học mới'})

@teacher_required
def edit_lesson(request, lesson_id):
    lesson = get_object_or_404(Lesson, id=lesson_id)
    course = lesson.course
    if not request.user.is_superuser and course.creator != request.user:
        messages.error(request, "Bạn không có quyền sửa bài học này.")
        return redirect('course_list')
        
    if request.method == 'POST':
        form = LessonForm(request.POST, instance=lesson)
        if form.is_valid():
            form.save()
            messages.success(request, f"Đã cập nhật bài học '{lesson.title}' thành công.")
            return redirect('manage_course_structure', course_id=course.id)
    else:
        form = LessonForm(instance=lesson)
    return render(request, 'lms/lesson_form.html', {'form': form, 'course': course, 'lesson': lesson, 'title': f"Sửa bài học: {lesson.title}"})

@teacher_required
def delete_lesson(request, lesson_id):
    lesson = get_object_or_404(Lesson, id=lesson_id)
    course = lesson.course
    if not request.user.is_superuser and course.creator != request.user:
        messages.error(request, "Bạn không có quyền xóa bài học này.")
        return redirect('course_list')
        
    course_id = course.id
    lesson.delete()
    messages.success(request, "Đã xóa bài học thành công.")
    return redirect('manage_course_structure', course_id=course_id)

@login_required
def buy_course(request, course_id):
    course = get_object_or_404(Course, id=course_id)
    # Check if already owned and active
    if CourseOwnership.has_active_ownership(request.user, course):
        messages.info(request, "Bạn đã sở hữu khóa học này và vẫn còn hạn học.")
        return redirect('course_detail', course_id=course.id)
        
    expires_at = None
    if course.duration_days and course.duration_days > 0:
        expires_at = timezone.now() + timezone.timedelta(days=course.duration_days)

    if course.price == 0:
        ownership, created = CourseOwnership.objects.get_or_create(
            user=request.user, 
            course=course,
            defaults={'expires_at': expires_at}
        )
        if not created:
            ownership.expires_at = expires_at
            ownership.purchased_at = timezone.now()
            ownership.save()
        messages.success(request, f"Đăng ký khóa học '{course.title}' thành công.")
    else:
        profile = request.user.profile
        if profile.wallet_balance < course.price:
            messages.error(request, "Số dư tài khoản không đủ để mua khóa học này. Vui lòng nạp thêm tiền.")
            return redirect('course_detail', course_id=course.id)
            
        with transaction.atomic():
            profile.wallet_balance -= course.price
            profile.save()
            
            ownership, created = CourseOwnership.objects.get_or_create(
                user=request.user, 
                course=course,
                defaults={'expires_at': expires_at}
            )
            if not created:
                ownership.expires_at = expires_at
                ownership.purchased_at = timezone.now()
                ownership.save()
            
            WalletTransaction.objects.create(
                user=request.user,
                amount=course.price,
                transaction_type='PAYMENT',
                status='APPROVED'
            )
        messages.success(request, f"Mua khóa học '{course.title}' thành công.")
    return redirect('course_detail', course_id=course.id)

import re

def parse_external_link_helper(link):
    link = link.strip().rstrip('*').strip()
    link_lower = link.lower()
    
    if 'hsgtin' in link_lower or 'on.hsgtin.vn' in link_lower:
        match = re.search(r'/problem/([^/?#]+)', link)
        if match:
            return 'hsgtin', match.group(1)
    elif 'vnoj' in link_lower or 'oj.vnoi.info' in link_lower:
        match = re.search(r'/problem/([^/?#]+)', link)
        if match:
            return 'vnoj', match.group(1)
    elif 'codeforces' in link_lower:
        match1 = re.search(r'/contest/(\d+)/problem/([^/?#]+)', link)
        if match1:
            return 'codeforces', f"{match1.group(1)}{match1.group(2)}"
        match2 = re.search(r'/problemset/problem/(\d+)/([^/?#]+)', link)
        if match2:
            return 'codeforces', f"{match2.group(1)}{match2.group(2)}"
    return None, None

def extract_exercise_info_helper(line):
    is_hard = '*' in line
    url_match = re.search(r'(https?://[^\s*]+)', line)
    if not url_match:
        return None, None, None, False
    url = url_match.group(1).rstrip(')>. *')
    platform, problem_code = parse_external_link_helper(url)
    if platform and problem_code:
        return url, platform, problem_code, is_hard
    return None, None, None, False

@login_required
def lesson_detail(request, course_id, lesson_id):
    course = get_object_or_404(Course, id=course_id)
    lesson = get_object_or_404(Lesson, id=lesson_id, course=course)
    
    # 1. Check ownership and expiry
    if not CourseOwnership.has_active_ownership(request.user, course):
        messages.error(request, "Quyền học khóa học này đã hết hạn hoặc bạn chưa đăng ký khóa học.")
        return redirect('course_detail', course_id=course.id)
        
    is_trial_user = request.user.is_superuser or request.user.is_staff or course.creator == request.user
        
    # 2. Sequential Mode check
    if course.learning_mode == 'SEQUENTIAL' and not is_trial_user:
        previous_lessons = course.lessons.filter(order_index__lt=lesson.order_index).order_by('order_index')
        for prev_l in previous_lessons:
            completed = LessonProgress.objects.filter(user=request.user, lesson=prev_l, is_completed=True).exists()
            if not completed:
                messages.warning(request, f"Bạn cần hoàn thành bài học '{prev_l.title}' trước để mở khóa bài học này.")
                return redirect('lesson_detail', course_id=course.id, lesson_id=prev_l.id)
                
    # 3. Load all lessons of the course for the sidebar
    lessons = course.lessons.all().order_by('order_index')
    
    # Calculate progress status of each lesson
    lessons_status = []
    for l in lessons:
        is_current = (l.id == lesson.id)
        is_completed = LessonProgress.objects.filter(user=request.user, lesson=l, is_completed=True).exists()
        
        is_locked = False
        if course.learning_mode == 'SEQUENTIAL' and l.order_index > lesson.order_index and not is_trial_user:
            # Check if there's any incomplete lesson before this one
            incomplete_before = course.lessons.filter(order_index__lt=l.order_index).exclude(
                id__in=LessonProgress.objects.filter(user=request.user, is_completed=True).values_list('lesson_id', flat=True)
            ).exists()
            if incomplete_before:
                is_locked = True
                
        lessons_status.append({
            'lesson': l,
            'is_current': is_current,
            'is_completed': is_completed,
            'is_locked': is_locked
        })
        
    # If the lesson is a TEST, let's load attempt info
    attempt_info = None
    if lesson.lesson_type == 'TEST' and lesson.test:
        active_attempt = Attempt.objects.filter(user=request.user, test=lesson.test, end_time__isnull=True).first()
        past_attempts = Attempt.objects.filter(user=request.user, test=lesson.test, end_time__isnull=False).order_by('-start_time')
        attempt_info = {
            'active_attempt': active_attempt,
            'past_attempts': past_attempts
        }
        
    # Fetch multi exercise links and progress
    multi_exercises = []
    if lesson.lesson_type == 'MULTI_EXERCISE':
        lines = lesson.content.split('\n')
        for line in lines:
            line_stripped = line.strip()
            if not line_stripped:
                continue
            url, platform, problem_code, is_hard = extract_exercise_info_helper(line_stripped)
            if platform and problem_code:
                completed = MultiExerciseProgress.objects.filter(
                    user=request.user,
                    lesson=lesson,
                    link=url,
                    is_completed=True
                ).exists()
                multi_exercises.append({
                    'url': url,
                    'platform': 'on.hsgtin.vn' if platform == 'hsgtin' else platform.upper(),
                    'problem_code': problem_code,
                    'is_hard': is_hard,
                    'is_completed': completed
                })

    context = {
        'course': course,
        'lesson': lesson,
        'lessons_status': lessons_status,
        'attempt_info': attempt_info,
        'multi_exercises': multi_exercises
    }
    return render(request, 'lms/lesson_detail.html', context)

@login_required
def complete_lesson_ajax(request, lesson_id):
    lesson = get_object_or_404(Lesson, id=lesson_id)
    if not CourseOwnership.has_active_ownership(request.user, lesson.course):
        return JsonResponse({'success': False, 'message': 'Không có quyền truy cập hoặc khóa học đã hết hạn.'}, status=403)
        
    # Giáo viên tạo bài (hay admin) học thử không ghi vào nhật ký học tập (không tạo LessonProgress)
    is_trial_user = request.user.is_superuser or request.user.is_staff or lesson.course.creator == request.user
    if not is_trial_user:
        progress, created = LessonProgress.objects.update_or_create(
            user=request.user,
            lesson=lesson,
            defaults={'is_completed': True, 'completed_at': timezone.now()}
        )
    
    # Find next lesson
    next_lesson = lesson.course.lessons.filter(order_index__gt=lesson.order_index).order_by('order_index').first()
    next_url = None
    if next_lesson:
        next_url = f"/courses/{lesson.course.id}/lessons/{next_lesson.id}/"
        
    return JsonResponse({
        'success': True,
        'next_url': next_url
    })

@login_required
def sync_lesson_progress_ajax(request, lesson_id):
    lesson = get_object_or_404(Lesson, id=lesson_id)
    if not CourseOwnership.has_active_ownership(request.user, lesson.course):
        return JsonResponse({'success': False, 'message': 'Không có quyền truy cập hoặc khóa học đã hết hạn.'}, status=403)
        
    if lesson.lesson_type != 'EXERCISE' or not lesson.external_problem_code:
        return JsonResponse({'success': False, 'message': 'Bài học này không liên kết bài tập ngoại vi.'}, status=400)
        
    profile = request.user.profile
    if not profile.codeforces_username and not profile.vnoj_username and not profile.dmoj_username:
        return JsonResponse({'success': False, 'message': 'Vui lòng cập nhật username VNOJ, Codeforces hoặc on.hsgtin.vn trong trang cá nhân của bạn.'}, status=400)
        
    success = False
    message = "Không tìm thấy bài nộp AC (chấp nhận) nào cho bài tập này."
    
    # Try syncing
    import requests
    link_lower = lesson.external_judge_link.lower() if lesson.external_judge_link else ""
    if 'codeforces' in link_lower and profile.codeforces_username:
        success = JudgeSyncService.sync_codeforces(profile.codeforces_username, lesson.external_problem_code)
    elif 'vnoj' in link_lower and profile.vnoj_username:
        success = JudgeSyncService.sync_vnoj(profile.vnoj_username, lesson.external_problem_code)
    elif ('hsgtin' in link_lower or 'on.hsgtin.vn' in link_lower) and profile.dmoj_username:
        success = JudgeSyncService.sync_hsgtin(profile.dmoj_username, lesson.external_problem_code)
    else:
        # Fallback: try matching platform or check everything configured
        if ('hsgtin' in link_lower or 'on.hsgtin.vn' in link_lower) and profile.dmoj_username:
            success = JudgeSyncService.sync_hsgtin(profile.dmoj_username, lesson.external_problem_code)
        elif 'vnoj' in link_lower and profile.vnoj_username:
            success = JudgeSyncService.sync_vnoj(profile.vnoj_username, lesson.external_problem_code)
        elif 'codeforces' in link_lower and profile.codeforces_username:
            success = JudgeSyncService.sync_codeforces(profile.codeforces_username, lesson.external_problem_code)
        else:
            if profile.dmoj_username:
                success = JudgeSyncService.sync_hsgtin(profile.dmoj_username, lesson.external_problem_code)
            if not success and profile.vnoj_username:
                success = JudgeSyncService.sync_vnoj(profile.vnoj_username, lesson.external_problem_code)
            if not success and profile.codeforces_username:
                success = JudgeSyncService.sync_codeforces(profile.codeforces_username, lesson.external_problem_code)
            
    if success:
        is_trial_user = request.user.is_superuser or request.user.is_staff or lesson.course.creator == request.user
        if not is_trial_user:
            LessonProgress.objects.update_or_create(
                user=request.user,
                lesson=lesson,
                defaults={'is_completed': True, 'completed_at': timezone.now()}
            )
        message = "Đồng bộ thành công! Bài học đã được hoàn thành."
        
    next_lesson = lesson.course.lessons.filter(order_index__gt=lesson.order_index).order_by('order_index').first()
    next_url = None
    if next_lesson:
        next_url = f"/courses/{lesson.course.id}/lessons/{next_lesson.id}/"
        
    return JsonResponse({
        'success': success,
        'message': message,
        'next_url': next_url
    })

@login_required
def sync_multi_exercise_ajax(request, lesson_id):
    lesson = get_object_or_404(Lesson, id=lesson_id)
    if not CourseOwnership.has_active_ownership(request.user, lesson.course):
        return JsonResponse({'success': False, 'message': 'Không có quyền truy cập hoặc khóa học đã hết hạn.'}, status=403)
        
    if lesson.lesson_type != 'MULTI_EXERCISE':
        return JsonResponse({'success': False, 'message': 'Bài học này không phải là bài đa bài tập.'}, status=400)
        
    import json
    try:
        data = json.loads(request.body)
        link = data.get('link', '').strip()
    except Exception:
        return JsonResponse({'success': False, 'message': 'Dữ liệu yêu cầu không hợp lệ.'}, status=400)
        
    if not link:
        return JsonResponse({'success': False, 'message': 'Thiếu đường dẫn bài tập.'}, status=400)
        
    # Parse link
    url, platform, problem_code, is_hard = extract_exercise_info_helper(link)
    if not platform or not problem_code:
        return JsonResponse({'success': False, 'message': 'Đường dẫn bài tập không đúng định dạng hỗ trợ (VNOJ, Codeforces, on.hsgtin.vn).'}, status=400)
        
    profile = request.user.profile
    if platform == 'codeforces' and not profile.codeforces_username:
        return JsonResponse({'success': False, 'message': 'Vui lòng cập nhật username Codeforces trong trang cá nhân của bạn.'}, status=400)
    elif platform == 'vnoj' and not profile.vnoj_username:
        return JsonResponse({'success': False, 'message': 'Vui lòng cập nhật username VNOJ trong trang cá nhân của bạn.'}, status=400)
    elif platform == 'hsgtin' and not profile.dmoj_username:
        return JsonResponse({'success': False, 'message': 'Vui lòng cập nhật username on.hsgtin.vn trong trang cá nhân của bạn.'}, status=400)
        
    success = False
    if platform == 'codeforces':
        success = JudgeSyncService.sync_codeforces(profile.codeforces_username, problem_code)
    elif platform == 'vnoj':
        success = JudgeSyncService.sync_vnoj(profile.vnoj_username, problem_code)
    elif platform == 'hsgtin':
        success = JudgeSyncService.sync_hsgtin(profile.dmoj_username, problem_code)
        
    if not success:
        return JsonResponse({'success': False, 'message': 'Không tìm thấy bài nộp AC (chấp nhận) nào cho bài tập này.'}, status=200)
        
    is_trial_user = request.user.is_superuser or request.user.is_staff or lesson.course.creator == request.user
    # Ghi nhận hoàn thành bài tập nhỏ (luôn lưu cho cả admin/giáo viên để hiển thị phản hồi trực quan khi chạy thử)
    MultiExerciseProgress.objects.update_or_create(
        user=request.user,
        lesson=lesson,
        link=url,
        defaults={'is_completed': True, 'completed_at': timezone.now()}
    )
        
    # Kiểm tra xem đã hoàn thành toàn bộ bài tập bắt buộc chưa
    lines = lesson.content.split('\n')
    required_urls = []
    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            continue
        e_url, e_platform, e_problem_code, e_is_hard = extract_exercise_info_helper(line_stripped)
        if e_platform and e_problem_code and not e_is_hard:
            required_urls.append(e_url)
            
    completed_urls = set(MultiExerciseProgress.objects.filter(
        user=request.user,
        lesson=lesson,
        is_completed=True
    ).values_list('link', flat=True))
    
    lesson_completed = all(req_url in completed_urls for req_url in required_urls)
    
    if lesson_completed:
        # Luôn ghi nhận hoàn tất bài học lớn (bao gồm cả admin/giáo viên học thử để đồng bộ checkmark tích xanh trên sidebar)
        LessonProgress.objects.update_or_create(
            user=request.user,
            lesson=lesson,
            defaults={'is_completed': True, 'completed_at': timezone.now()}
        )
        
    next_url = None
    if lesson_completed:
        next_lesson = lesson.course.lessons.filter(order_index__gt=lesson.order_index).order_by('order_index').first()
        if next_lesson:
            next_url = f"/courses/{lesson.course.id}/lessons/{next_lesson.id}/"
            
    return JsonResponse({
        'success': True,
        'message': 'Đồng bộ thành công! Bài tập này đã được hoàn thành.',
        'lesson_completed': lesson_completed,
        'next_url': next_url
    })

@login_required
def wallet_deposit(request):
    if request.method == 'POST':
        amount_str = request.POST.get('amount', '0')
        try:
            amount = float(amount_str)
            if amount <= 0:
                raise ValueError()
        except ValueError:
            messages.error(request, "Số tiền nạp không hợp lệ.")
            return redirect('wallet_deposit')
            
        proof = request.FILES.get('proof_image')
        WalletTransaction.objects.create(
            user=request.user,
            amount=amount,
            transaction_type='DEPOSIT',
            status='PENDING',
            proof_image=proof
        )
        messages.success(request, "Yêu cầu nạp tiền đã được gửi. Vui lòng chờ Admin phê duyệt.")
        return redirect('wallet_deposit')
        
    transactions = WalletTransaction.objects.filter(user=request.user).order_by('-created_at')
    return render(request, 'lms/wallet_deposit.html', {
        'transactions': transactions
    })

@login_required
def edit_profile(request):
    user = request.user
    profile, created = Profile.objects.get_or_create(user=user)
    
    if request.method == 'POST':
        user_form = UserEditForm(request.POST, instance=user)
        profile_form = ProfileEditForm(request.POST, instance=profile)
        if user_form.is_valid() and profile_form.is_valid():
            user_form.save()
            profile_form.save()
            messages.success(request, "Cập nhật thông tin cá nhân thành công!")
            return redirect('edit_profile')
        else:
            messages.error(request, "Có lỗi xảy ra, vui lòng kiểm tra lại thông tin.")
    else:
        user_form = UserEditForm(instance=user)
        profile_form = ProfileEditForm(instance=profile)
        
    return render(request, 'lms/edit_profile.html', {
        'user_form': user_form,
        'profile_form': profile_form,
        'profile': profile
    })

@login_required
def register_test(request, test_id):
    test = get_object_or_404(Test, id=test_id)
    profile = request.user.profile
    
    existing_registration = TestOwnership.objects.filter(user=request.user, test=test).first()
    if existing_registration and existing_registration.agreed_rules:
        messages.info(request, "Bạn đã đăng ký dự thi bài thi này thành công trước đó.")
        return redirect('test_detail', test_id=test.id)
        
    if request.method == 'POST':
        agree = request.POST.get('agree_rules') == 'on'
        if not agree:
            messages.error(request, "Bạn phải đọc và tích chọn đồng ý với Quy chế phòng thi trước khi đăng ký.")
            return redirect('test_detail', test_id=test.id)
            
        if test.price > 0:
            if profile.wallet_balance < test.price:
                messages.error(request, "Số dư tài khoản không đủ để thanh toán lệ phí thi. Vui lòng nạp thêm tiền.")
                return redirect('wallet_deposit')
                
            with transaction.atomic():
                profile.wallet_balance -= test.price
                profile.save()
                
                TestOwnership.objects.update_or_create(
                    user=request.user, test=test,
                    defaults={
                        'registered_at': timezone.now(),
                        'agreed_rules': True
                    }
                )
                
                WalletTransaction.objects.create(
                    user=request.user,
                    amount=test.price,
                    transaction_type='PAYMENT',
                    status='APPROVED'
                )
                
            messages.success(request, f"Đăng ký dự thi và thanh toán thành công lệ phí {test.price}đ cho kỳ thi: {test.title}")
        else:
            TestOwnership.objects.update_or_create(
                user=request.user, test=test,
                defaults={
                    'registered_at': timezone.now(),
                    'agreed_rules': True
                }
            )
            messages.success(request, f"Đăng ký tham gia kì thi thành công: {test.title}")
            
        return redirect('test_detail', test_id=test.id)
        
    return redirect('test_detail', test_id=test.id)

@login_required
def take_test(request, test_id):
    test = get_object_or_404(Test, id=test_id)
    now = timezone.now()
    
    is_exam_over = test.is_official and test.end_time and now > test.end_time
    
    # Bỏ qua kiểm tra mua đề thi / đăng ký thi nếu user là admin, staff, hoặc người tạo đề thi / người tạo khóa học liên kết hoặc giáo viên
    is_bypass_test = (
        request.user.is_superuser 
        or request.user.is_staff 
        or test.creator == request.user
        or (hasattr(request.user, 'profile') and (request.user.profile.can_create_exams or request.user.profile.can_create_courses))
    )
    if not is_bypass_test:
        from lms.models import Lesson, ClassroomMembership
        # Check if accessing in the context of a classroom lesson via lesson_id
        lesson_id = request.GET.get('lesson_id') or request.POST.get('lesson_id')
        if lesson_id:
            lesson_context = Lesson.objects.filter(id=lesson_id, test=test).first()
            if lesson_context and ClassroomMembership.objects.filter(
                student=request.user,
                status='APPROVED',
                classroom__items__lesson=lesson_context
            ).exists():
                is_bypass_test = True

        if not is_bypass_test:
            associated_lessons = Lesson.objects.filter(test=test)
            for al in associated_lessons:
                if al.course.creator == request.user or CourseOwnership.has_active_ownership(request.user, al.course):
                    is_bypass_test = True
                    break

    
    if test.is_official:
        if is_exam_over:
            # Hết giờ thi chính thức -> Cho phép làm bài luyện tập (phát sinh lượt làm bài tự do)
            if not is_bypass_test and test.price > 0 and not TestOwnership.objects.filter(user=request.user, test=test).exists():
                messages.error(request, "Bạn cần mua đề thi này để làm bài luyện tập.")
                return redirect('test_detail', test_id=test.id)
            is_attempt_official = False
        else:
            # Đang trong thời gian thi chính thức
            registration = TestOwnership.objects.filter(user=request.user, test=test).first()
            if not is_bypass_test and (not registration or not registration.agreed_rules):
                messages.error(request, "Bạn chưa hoàn tất thủ tục đăng ký dự thi và đồng ý quy chế phòng thi.")
                return redirect('test_detail', test_id=test.id)
                
            if test.start_time and now < test.start_time:
                messages.error(request, "Kỳ thi chưa bắt đầu. Vui lòng quay lại làm bài đúng giờ.")
                return redirect('test_detail', test_id=test.id)
                
            # Ngăn chặn thi lại nhiều lần trong thời gian thi chính thức nếu đã nộp bài thi chính thức
            if Attempt.objects.filter(user=request.user, test=test, is_official=True, end_time__isnull=False).exists():
                messages.error(request, "Bạn đã hoàn thành bài thi chính thức và không thể làm lại trong thời gian kỳ thi đang diễn ra.")
                return redirect('test_detail', test_id=test.id)
                
            is_attempt_official = True
    else:
        # Đề thi luyện tập thông thường
        if not is_bypass_test and test.price > 0 and not TestOwnership.objects.filter(user=request.user, test=test).exists():
            messages.error(request, "Bạn cần thanh toán phí đề thi này trước khi làm bài.")
            return redirect('test_detail', test_id=test.id)
        is_attempt_official = False
    
    attempt, created = Attempt.objects.get_or_create(
        user=request.user, test=test, end_time__isnull=True,
        defaults={'is_official': is_attempt_official}
    )
    
    parts_to_render = []
    is_dynamic = hasattr(test, 'dynamictest')

    if is_dynamic:
        dt = test.dynamictest
        # Nếu là lượt mới, hoặc chưa có shuffled_data
        if created or not attempt.shuffled_data:
            # Keep track of selected questions to avoid duplicate or equivalent group clashes
            selected_questions = []
            selected_q_ids = set()
            selected_equiv_groups = set()

            # 1. Parse and add explicit question IDs
            explicit_ids = []
            if dt.dynamic_question_ids:
                import re
                raw_ids = re.split(r'[\s,\t\r\n]+', dt.dynamic_question_ids)
                for rid in raw_ids:
                    rid = rid.strip()
                    if rid.isdigit():
                        qid = int(rid)
                        q = Question.objects.filter(id=qid).first()
                        if q:
                            eq_id = q.equivalent_group_id
                            if eq_id and eq_id in selected_equiv_groups:
                                continue
                            selected_questions.append(q)
                            selected_q_ids.add(q.id)
                            explicit_ids.append(q.id)
                            if eq_id:
                                selected_equiv_groups.add(eq_id)
                                
            # 2. Parse and sample from group rules
            if dt.dynamic_group_rules:
                import re
                rules = re.split(r'[\s,;\t]+', dt.dynamic_group_rules)
                for rule in rules:
                    rule = rule.strip()
                    if not rule:
                        continue
                    if ':' in rule:
                        parts = rule.split(':')
                        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                            g_id = int(parts[0])
                            count = int(parts[1])
                            
                            candidates = list(Question.objects.filter(group_id=g_id))
                            random.shuffle(candidates)
                            
                            sampled_count = 0
                            for q in candidates:
                                if sampled_count >= count:
                                    break
                                if q.id in selected_q_ids:
                                    continue
                                eq_id = q.equivalent_group_id
                                if eq_id and eq_id in selected_equiv_groups:
                                    continue
                                    
                                selected_questions.append(q)
                                selected_q_ids.add(q.id)
                                if eq_id:
                                    selected_equiv_groups.add(eq_id)
                                sampled_count += 1
                                
            all_selected_ids = [q.id for q in selected_questions]
            
            # 3. Giới hạn số câu tối đa (ưu tiên giữ câu chỉ định trực tiếp)
            if len(all_selected_ids) > dt.max_questions:
                explicit_set = set(explicit_ids)
                group_only_ids = [qid for qid in all_selected_ids if qid not in explicit_set]
                needed = dt.max_questions - len(explicit_ids)
                if needed > 0:
                    all_selected_ids = explicit_ids + group_only_ids[:needed]
                else:
                    all_selected_ids = explicit_ids[:dt.max_questions]
            
            # 4. Đảo ngẫu nhiên câu hỏi
            random.shuffle(all_selected_ids)
            
            # 6. Tính điểm chia đều
            num_questions = len(all_selected_ids)
            points_per_question = 0.0
            if num_questions > 0:
                points_per_question = round(dt.total_points / num_questions, 2)
                
            # 7. Lưu vào shuffled_data
            shuffled_data = {"1": []}
            for q_id in all_selected_ids:
                question = Question.objects.filter(id=q_id).first()
                if not question:
                    continue
                choices = list(question.choices.all())
                pos_groups = {}
                for c in choices:
                    pos_groups.setdefault(c.position, []).append(c.id)
                
                shuffled_choices = []
                for pos in sorted(pos_groups.keys()):
                    group = pos_groups[pos]
                    random.shuffle(group)
                    shuffled_choices.extend(group)
                    
                q_info = {
                    'question_id': q_id,
                    'choices': shuffled_choices,
                    'points': points_per_question,
                    'part_number': 1
                }
                shuffled_data["1"].append(q_info)
            attempt.shuffled_data = shuffled_data
            attempt.save()

        # Phẳng hóa câu hỏi đã xáo trộn và dựng MockTestQuestion
        all_q_info = []
        for part_key in sorted([k for k in attempt.shuffled_data.keys() if k != '_part_order'], key=lambda x: int(x)):
            all_q_info.extend(attempt.shuffled_data[part_key])
            
        questions_by_part = {}
        for q_info in all_q_info:
            q_id = q_info['question_id']
            question = Question.objects.filter(id=q_id).first()
            if not question:
                continue
                
            class MockTestQuestion:
                def __init__(self, id, question, points, part_number):
                    self.id = id
                    self.question = question
                    self.points = points
                    self.part_number = part_number
                    self.optional_type = 'NONE'
                def get_optional_type_display(self):
                    return 'NONE'
                    
            tq = MockTestQuestion(
                id=q_id,
                question=question,
                points=q_info.get('points', 1.0),
                part_number=q_info.get('part_number', 1)
            )
            
            from lms.bbcode_parser import BBBienParser
            parser = BBBienParser(seed=attempt.id * 100000 + question.id)
            question.content = parser.parse(question.content)
            
            all_choices = list(question.choices.all())
            for c in all_choices:
                c.content = parser.parse(c.content)
            choice_map = {c.id: c for c in all_choices}
            current_choice_ids = set(choice_map.keys())
            shuffled_ids = q_info.get('choices', [])
            
            if not shuffled_ids or set(shuffled_ids) != current_choice_ids:
                choices_list = all_choices
                pos_groups = {}
                for c in choices_list:
                    pos_groups.setdefault(c.position, []).append(c.id)
                new_shuffled_ids = []
                for pos in sorted(pos_groups.keys()):
                    group = pos_groups[pos]
                    random.shuffle(group)
                    new_shuffled_ids.extend(group)
                q_info['choices'] = new_shuffled_ids
                attempt.save()
                shuffled_ids = new_shuffled_ids
                
            ordered_choices = [choice_map[cid] for cid in shuffled_ids if cid in choice_map]
            
            existing_ans = AttemptAnswer.objects.filter(attempt=attempt, question=question).first()
            questions_with_choices = {
                'tq': tq,
                'choices': ordered_choices,
                'answer': existing_ans
            }
            
            part_num = tq.part_number
            if part_num not in questions_by_part:
                questions_by_part[part_num] = []
            questions_by_part[part_num].append(questions_with_choices)

        part_instructions = {}  # Đề động chỉ dùng mặc định 1 phần, không cần cấu hình hướng dẫn phức tạp
    else:
        # Nếu là lượt mới, khởi tạo dữ liệu đảo câu hỏi/đáp án
        if created or not attempt.shuffled_data:
            # Sắp xếp theo order_index và xáo trộn ngẫu nhiên nội bộ các câu bằng thứ tự
            questions = list(TestQuestion.objects.filter(test=test).select_related('question').order_by('order_index', '?'))
            
            shuffled_data = {}
            for tq in questions:
                # Đảo đáp án dựa trên position
                choices = list(tq.question.choices.all())
                pos_groups = {}
                for c in choices:
                    pos_groups.setdefault(c.position, []).append(c.id)
                
                shuffled_choices = []
                for pos in sorted(pos_groups.keys()):
                    group = pos_groups[pos]
                    random.shuffle(group)
                    shuffled_choices.extend(group)
                
                q_info = {
                    'tq_id': tq.id,
                    'choices': shuffled_choices
                }
                part_key = str(tq.part_number)
                if part_key not in shuffled_data:
                    shuffled_data[part_key] = []
                shuffled_data[part_key].append(q_info)
            
            # Đảo ngẫu nhiên thứ tự phần thi nếu có tùy chọn và có nhiều hơn 1 phần thi
            part_keys = [k for k in shuffled_data.keys() if k != '_part_order']
            if test.shuffle_parts and len(part_keys) > 1:
                random.shuffle(part_keys)
            else:
                part_keys = sorted(part_keys, key=lambda x: int(x))
            shuffled_data['_part_order'] = part_keys
            
            attempt.shuffled_data = shuffled_data
            attempt.save()

        # Chuẩn bị dữ liệu hiển thị dựa trên shuffled_data và cấu trúc phần thi thực tế
        parts_to_render = []
        part_instructions = {
            pi.part_number: pi.instruction
            for pi in TestPartInstruction.objects.filter(test=test).select_related('instruction')
        }
        
        # Lấy nhanh tất cả TestQuestion hiện tại của đề thi
        tqs_by_id = {
            tq.id: tq 
            for tq in TestQuestion.objects.filter(test=test).select_related('question')
        }
        
        # Tự động đồng bộ hóa nếu giáo viên thêm câu hỏi mới sau khi học sinh đã mở đề
        shuffled_modified = False
        for tq in tqs_by_id.values():
            found = False
            for part_key, q_list in attempt.shuffled_data.items():
                if part_key == '_part_order':
                    continue
                if any(q['tq_id'] == tq.id for q in q_list):
                    found = True
                    break
            if not found:
                choices = list(tq.question.choices.all())
                pos_groups = {}
                for c in choices:
                    pos_groups.setdefault(c.position, []).append(c.id)
                
                shuffled_choices = []
                for pos in sorted(pos_groups.keys()):
                    group = pos_groups[pos]
                    random.shuffle(group)
                    shuffled_choices.extend(group)
                    
                q_info = {
                    'tq_id': tq.id,
                    'choices': shuffled_choices
                }
                part_key = str(tq.part_number)
                attempt.shuffled_data.setdefault(part_key, []).append(q_info)
                shuffled_modified = True
                
                # Cập nhật _part_order nếu chưa tồn tại part_key
                if '_part_order' in attempt.shuffled_data:
                    if part_key not in attempt.shuffled_data['_part_order']:
                        attempt.shuffled_data['_part_order'].append(part_key)
                        if not test.shuffle_parts:
                            attempt.shuffled_data['_part_order'] = sorted(attempt.shuffled_data['_part_order'], key=lambda x: int(x))

        if shuffled_modified:
            attempt.save()
            
        # Phẳng hóa câu hỏi đã xáo trộn
        all_q_info = []
        # Duy trì thứ tự phần thi đã xáo trộn hoặc gốc của attempt khi phẳng hóa
        part_order = attempt.shuffled_data.get('_part_order')
        if not test.shuffle_parts or not part_order:
            part_order = sorted([k for k in attempt.shuffled_data.keys() if k != '_part_order'], key=lambda x: int(x))
            
        for part_key in part_order:
            if part_key in attempt.shuffled_data:
                all_q_info.extend(attempt.shuffled_data[part_key])
            
        questions_by_part = {}
        for q_info in all_q_info:
            tq = tqs_by_id.get(q_info['tq_id'])
            if not tq:
                continue  # Bỏ qua câu hỏi đã bị giáo viên gỡ khỏi đề thi
                
            from lms.bbcode_parser import BBBienParser
            parser = BBBienParser(seed=attempt.id * 100000 + tq.question.id)
            tq.question.content = parser.parse(tq.question.content)
            
            all_choices = list(tq.question.choices.all())
            for c in all_choices:
                c.content = parser.parse(c.content)
            choice_map = {c.id: c for c in all_choices}
            current_choice_ids = set(choice_map.keys())
            shuffled_ids = q_info.get('choices', [])
            
            # Nếu có sai lệch phương án (thêm/bớt đáp án hoặc rỗng), tiến hành trộn và đồng bộ lại
            if not shuffled_ids or set(shuffled_ids) != current_choice_ids:
                choices_list = all_choices
                pos_groups = {}
                for c in choices_list:
                    pos_groups.setdefault(c.position, []).append(c.id)
                
                new_shuffled_ids = []
                for pos in sorted(pos_groups.keys()):
                    group = pos_groups[pos]
                    random.shuffle(group)
                    new_shuffled_ids.extend(group)
                
                q_info['choices'] = new_shuffled_ids
                attempt.save()
                shuffled_ids = new_shuffled_ids
                
            ordered_choices = [choice_map[cid] for cid in shuffled_ids if cid in choice_map]
            
            # Lấy câu trả lời đã lưu (nếu có)
            existing_ans = AttemptAnswer.objects.filter(attempt=attempt, test_question=tq).first()
            
            questions_with_choices = {
                'tq': tq,
                'choices': ordered_choices,
                'answer': existing_ans
            }
            
            part_num = tq.part_number
            if part_num not in questions_by_part:
                questions_by_part[part_num] = []
            questions_by_part[part_num].append(questions_with_choices)
        
    part_order = attempt.shuffled_data.get('_part_order')
    if not test.shuffle_parts or not part_order:
        part_order = sorted([k for k in attempt.shuffled_data.keys() if k != '_part_order'], key=lambda x: int(x))
        
    display_idx = 1
    for part_key in part_order:
        part_num = int(part_key)
        if part_num in questions_by_part:
            instruction = part_instructions.get(part_num)
            parts_to_render.append({
                'part': {
                    'id': part_num,
                    'title_roman': int_to_roman(display_idx),
                    'instruction': instruction.content if instruction else ""
                },
                'questions': questions_by_part[part_num]
            })
            display_idx += 1

    # Calculate remaining time (seconds) based on start_time and test duration
    elapsed = (timezone.now() - attempt.start_time).total_seconds()
    duration_seconds = (test.duration or 60) * 60
    time_left = max(0, int(duration_seconds - elapsed))

    return render(request, 'lms/take_test.html', {
        'test': test,
        'attempt': attempt,
        'parts': parts_to_render,
        'time_left': time_left,
    })

@login_required
def submit_test(request, attempt_id):
    attempt = get_object_or_404(Attempt, id=attempt_id, user=request.user)
    if attempt.end_time:
        lesson_id = request.GET.get('lesson_id')
        if lesson_id:
            return redirect(f"{reverse('test_result', args=[attempt.id])}?lesson_id={lesson_id}")
        return redirect('test_result', attempt_id=attempt.id)
    
    if request.method == 'POST':
        # Cập nhật số lần và thời gian rời trang từ form gửi lên
        attempt.left_page_count = int(request.POST.get('left_page_count', attempt.left_page_count))
        attempt.left_page_time = int(request.POST.get('left_page_time', attempt.left_page_time))
        attempt.save()

        if hasattr(attempt.test, 'dynamictest'):
            # Đối với đề thi động, các câu hỏi và thứ tự được lưu trong attempt.shuffled_data
            for part_key, q_list in attempt.shuffled_data.items():
                if part_key == '_part_order':
                    continue
                for q_info in q_list:
                    q_id = q_info['question_id']
                    points = q_info.get('points', 1.0)
                    key = f"q_{q_id}"
                    
                    question = Question.objects.filter(id=q_id).first()
                    if not question:
                        continue
                        
                    if question.question_type in [1, 2, 3]:
                        choice_ids = request.POST.getlist(key)
                        if choice_ids or AttemptAnswer.objects.filter(attempt=attempt, question=question).exists():
                            ans, _ = AttemptAnswer.objects.get_or_create(
                                attempt=attempt, question=question,
                                defaults={'points': points}
                            )
                            ans.selected_choices.set(choice_ids)
                            ans.save()
                    elif question.question_type == 4:
                        value = request.POST.get(key, "").strip()
                        if value or AttemptAnswer.objects.filter(attempt=attempt, question=question).exists():
                            ans, _ = AttemptAnswer.objects.get_or_create(
                                attempt=attempt, question=question,
                                defaults={'points': points}
                            )
                            ans.short_answer = value
                            ans.save()
        else:
            # Duyệt qua tất cả TestQuestion của đề thi để lưu/cập nhật chính xác kể cả khi không tích chọn câu nào
            test_questions = TestQuestion.objects.filter(test=attempt.test)
            for tq in test_questions:
                key = f"q_{tq.id}"
                
                if tq.question.question_type in [1, 2, 3]:
                    # Nếu không chọn ý nào, getlist() sẽ trả về danh sách rỗng []
                    choice_ids = request.POST.getlist(key)
                    
                    # Lưu nếu có câu trả lời gửi lên hoặc trước đó đã tồn tại bản ghi
                    if choice_ids or AttemptAnswer.objects.filter(attempt=attempt, test_question=tq).exists():
                        ans, _ = AttemptAnswer.objects.get_or_create(attempt=attempt, test_question=tq)
                        ans.selected_choices.set(choice_ids)
                        ans.save()
                elif tq.question.question_type == 4:
                    value = request.POST.get(key, "").strip()
                    
                    if value or AttemptAnswer.objects.filter(attempt=attempt, test_question=tq).exists():
                        ans, _ = AttemptAnswer.objects.get_or_create(attempt=attempt, test_question=tq)
                        ans.short_answer = value
                        ans.save()
        
        # Chấm điểm
        ScoringService.calculate_attempt_score(attempt)
        
        # Tự động hoàn thành bài kiểm tra liên kết trong khóa học (bỏ qua nếu giáo viên tạo bài hoặc admin làm thử)
        associated_lessons = Lesson.objects.filter(lesson_type='TEST', test=attempt.test)
        for lesson in associated_lessons:
            is_trial_user = request.user.is_superuser or request.user.is_staff or lesson.course.creator == request.user
            if not is_trial_user:
                # Nếu là khóa học tuần tự, yêu cầu học sinh làm đúng 100% điểm bài thi mới được hoàn thành
                if lesson.course.learning_mode == 'SEQUENTIAL':
                    max_score = lesson.test.total_possible_points
                    is_perfect = attempt.total_score >= (max_score - 1e-5)
                    if is_perfect:
                        LessonProgress.objects.update_or_create(
                            user=request.user,
                            lesson=lesson,
                            defaults={'is_completed': True, 'completed_at': timezone.now()}
                        )
                else:
                    # Với khóa học tự do, chỉ cần nộp bài là hoàn thành
                    LessonProgress.objects.update_or_create(
                        user=request.user,
                        lesson=lesson,
                        defaults={'is_completed': True, 'completed_at': timezone.now()}
                    )
            
        messages.success(request, "Nộp bài thành công!")
        lesson_id = request.GET.get('lesson_id')
        if lesson_id:
            return redirect(f"{reverse('test_result', args=[attempt.id])}?lesson_id={lesson_id}")
        return redirect('test_result', attempt_id=attempt.id)
    
    lesson_id = request.GET.get('lesson_id')
    if lesson_id:
        return redirect(f"{reverse('take_test', args=[attempt.test.id])}?lesson_id={lesson_id}")
    return redirect('take_test', test_id=attempt.test.id)

@login_required
def test_result(request, attempt_id):
    attempt = get_object_or_404(Attempt, id=attempt_id)
    if attempt.user != request.user and not request.user.is_staff:
        raise Http404("Bạn không có quyền xem kết quả này.")
        
    context = {'attempt': attempt}
    lesson_id = request.GET.get('lesson_id')
    lesson = None
    if lesson_id:
        lesson = Lesson.objects.filter(id=lesson_id).select_related('course').first()
    
    if not lesson:
        associated_lessons = Lesson.objects.filter(lesson_type='TEST', test=attempt.test).select_related('course')
        if associated_lessons.exists():
            lesson = associated_lessons.first()
            
    if lesson:
        context['lesson'] = lesson
        
    return render(request, 'lms/test_result.html', context)

@login_required
def review_attempt(request, attempt_id):
    attempt = get_object_or_404(Attempt, id=attempt_id)
    if attempt.user != request.user and not request.user.is_staff:
        raise Http404("Bạn không có quyền xem lại lượt thi này.")
    
    parts_to_render = []
    is_dynamic = hasattr(attempt.test, 'dynamictest')

    if is_dynamic:
        # Phẳng hóa câu hỏi đã xáo trộn từ shuffled_data cho đề động
        all_q_info = []
        for part_key in sorted([k for k in attempt.shuffled_data.keys() if k != '_part_order'], key=lambda x: int(x)):
            all_q_info.extend(attempt.shuffled_data[part_key])
            
        questions_by_part = {}
        for q_info in all_q_info:
            q_id = q_info['question_id']
            question = Question.objects.filter(id=q_id).first()
            if not question:
                continue
                
            class MockTestQuestion:
                def __init__(self, id, question, points, part_number):
                    self.id = id
                    self.question = question
                    self.points = points
                    self.part_number = part_number
                    self.optional_type = 'NONE'
                def get_optional_type_display(self):
                    return 'NONE'
                    
            tq = MockTestQuestion(
                id=q_id,
                question=question,
                points=q_info.get('points', 1.0),
                part_number=q_info.get('part_number', 1)
            )
            
            from lms.bbcode_parser import BBBienParser
            parser = BBBienParser(seed=attempt.id * 100000 + question.id)
            question.content = parser.parse(question.content)
            
            all_choices = list(question.choices.all())
            for c in all_choices:
                c.content = parser.parse(c.content)
            choice_map = {c.id: c for c in all_choices}
            current_choice_ids = set(choice_map.keys())
            shuffled_ids = q_info.get('choices', [])
            
            if not shuffled_ids or set(shuffled_ids) != current_choice_ids:
                choices_list = all_choices
                pos_groups = {}
                for c in choices_list:
                    pos_groups.setdefault(c.position, []).append(c.id)
                new_shuffled_ids = []
                for pos in sorted(pos_groups.keys()):
                    group = pos_groups[pos]
                    random.shuffle(group)
                    new_shuffled_ids.extend(group)
                q_info['choices'] = new_shuffled_ids
                attempt.save()
                shuffled_ids = new_shuffled_ids
                
            ordered_choices = [choice_map[cid] for cid in shuffled_ids if cid in choice_map]
            
            ans = AttemptAnswer.objects.filter(attempt=attempt, question=question).first()
            
            questions_with_results = {
                'tq': tq,
                'choices': ordered_choices,
                'answer': ans,
                'correct_choices': [choice_map[c.id] for c in all_choices] if question.question_type == 4 else [choice_map[c.id] for c in all_choices if c.is_correct]
            }
            
            part_num = tq.part_number
            if part_num not in questions_by_part:
                questions_by_part[part_num] = []
            questions_by_part[part_num].append(questions_with_results)
            
        part_instructions = {}
    else:
        # Logic tương tự take_test nhưng hiển thị kết quả dựa trên cấu trúc phần thi thực tế
        part_instructions = {
            pi.part_number: pi.instruction
            for pi in TestPartInstruction.objects.filter(test=attempt.test).select_related('instruction')
        }
        
        # Lấy nhanh tất cả TestQuestion hiện tại của đề thi
        tqs_by_id = {
            tq.id: tq 
            for tq in TestQuestion.objects.filter(test=attempt.test).select_related('question')
        }
        
        # Tự động đồng bộ hóa nếu giáo viên thêm câu hỏi mới sau khi học sinh đã mở đề
        shuffled_modified = False
        for tq in tqs_by_id.values():
            found = False
            for part_key, q_list in attempt.shuffled_data.items():
                if part_key == '_part_order':
                    continue
                if any(q['tq_id'] == tq.id for q in q_list):
                    found = True
                    break
            if not found:
                choices = list(tq.question.choices.all())
                pos_groups = {}
                for c in choices:
                    pos_groups.setdefault(c.position, []).append(c.id)
                
                shuffled_choices = []
                for pos in sorted(pos_groups.keys()):
                    group = pos_groups[pos]
                    random.shuffle(group)
                    shuffled_choices.extend(group)
                    
                q_info = {
                    'tq_id': tq.id,
                    'choices': shuffled_choices
                }
                part_key = str(tq.part_number)
                attempt.shuffled_data.setdefault(part_key, []).append(q_info)
                shuffled_modified = True
                
                # Cập nhật _part_order nếu chưa tồn tại part_key
                if '_part_order' in attempt.shuffled_data:
                    if part_key not in attempt.shuffled_data['_part_order']:
                        attempt.shuffled_data['_part_order'].append(part_key)
                        if not attempt.test.shuffle_parts:
                            attempt.shuffled_data['_part_order'] = sorted(attempt.shuffled_data['_part_order'], key=lambda x: int(x))

        if shuffled_modified:
            attempt.save()
            
        # Phẳng hóa câu hỏi đã xáo trộn
        all_q_info = []
        # Duy trì thứ tự phần thi gốc hoặc đã xáo trộn của attempt khi phẳng hóa
        part_order = attempt.shuffled_data.get('_part_order')
        if not attempt.test.shuffle_parts or not part_order:
            part_order = sorted([k for k in attempt.shuffled_data.keys() if k != '_part_order'], key=lambda x: int(x))
            
        for part_key in part_order:
            if part_key in attempt.shuffled_data:
                all_q_info.extend(attempt.shuffled_data[part_key])
            
        questions_by_part = {}
        for q_info in all_q_info:
            tq = tqs_by_id.get(q_info['tq_id'])
            if not tq:
                continue  # Bỏ qua câu hỏi đã bị giáo viên gỡ khỏi đề thi
                
            from lms.bbcode_parser import BBBienParser
            parser = BBBienParser(seed=attempt.id * 100000 + tq.question.id)
            tq.question.content = parser.parse(tq.question.content)
            
            all_choices = list(tq.question.choices.all())
            for c in all_choices:
                c.content = parser.parse(c.content)
            choice_map = {c.id: c for c in all_choices}
            current_choice_ids = set(choice_map.keys())
            shuffled_ids = q_info.get('choices', [])
            
            # Nếu có sai lệch phương án (thêm/bớt đáp án hoặc rỗng), tiến hành trộn và đồng bộ lại
            if not shuffled_ids or set(shuffled_ids) != current_choice_ids:
                choices_list = all_choices
                pos_groups = {}
                for c in choices_list:
                    pos_groups.setdefault(c.position, []).append(c.id)
                
                new_shuffled_ids = []
                for pos in sorted(pos_groups.keys()):
                    group = pos_groups[pos]
                    random.shuffle(group)
                    new_shuffled_ids.extend(group)
                
                q_info['choices'] = new_shuffled_ids
                attempt.save()
                shuffled_ids = new_shuffled_ids
                
            ordered_choices = [choice_map[cid] for cid in shuffled_ids if cid in choice_map]
            
            ans = AttemptAnswer.objects.filter(attempt=attempt, test_question=tq).first()
            
            questions_with_results = {
                'tq': tq,
                'choices': ordered_choices,
                'answer': ans,
                'correct_choices': [choice_map[c.id] for c in all_choices] if tq.question.question_type == 4 else [choice_map[c.id] for c in all_choices if c.is_correct]
            }
            
            part_num = tq.part_number
            if part_num not in questions_by_part:
                questions_by_part[part_num] = []
            questions_by_part[part_num].append(questions_with_results)
            
    part_order = attempt.shuffled_data.get('_part_order')
    if not attempt.test.shuffle_parts or not part_order:
        part_order = sorted([k for k in attempt.shuffled_data.keys() if k != '_part_order'], key=lambda x: int(x))
        
    display_idx = 1
    for part_key in part_order:
        part_num = int(part_key)
        if part_num in questions_by_part:
            instruction = part_instructions.get(part_num)
            parts_to_render.append({
                'part': {
                    'id': part_num,
                    'title_roman': int_to_roman(display_idx),
                    'instruction': instruction.content if instruction else ""
                },
                'questions': questions_by_part[part_num]
            })
            display_idx += 1

    return render(request, 'lms/review_attempt.html', {
        'attempt': attempt,
        'parts': parts_to_render
    })

@login_required
def leaderboard(request, test_id):
    test = get_object_or_404(Test, id=test_id)
    mode = request.GET.get('mode', 'official')
    if mode not in ['official', 'all']:
        mode = 'official'
    
    attempts = Attempt.objects.filter(test=test, end_time__isnull=False).select_related('user')
    
    if mode == 'official':
        # Chỉ lấy các lượt thi chính thức
        attempts = attempts.filter(is_official=True)
        
    # Lọc bỏ các lượt làm thử của admin/giáo viên tạo đề/khóa học liên kết khỏi bảng xếp hạng
    filtered_attempts = []
    for attempt in attempts:
        is_trial = (
            attempt.user.is_superuser 
            or attempt.user.is_staff 
            or attempt.test.creator == attempt.user
        )
        if not is_trial:
            from .models import Lesson
            if Lesson.objects.filter(test=attempt.test, course__creator=attempt.user).exists():
                is_trial = True
        if not is_trial:
            filtered_attempts.append(attempt)
            
    attempts = filtered_attempts
    
    # Lọc lượt làm bài tốt nhất của mỗi học sinh theo 3 tiêu chí xếp hạng:
    # 1. Điểm cao nhất
    # 2. Thời gian làm bài ngắn nhất (end_time - start_time)
    # 3. Thời điểm bắt đầu làm bài sớm nhất (start_time)
    user_best_attempts = {}
    for attempt in attempts:
        username = attempt.user.username
        duration = (attempt.end_time - attempt.start_time).total_seconds()
        attempt.duration_seconds = duration
        
        if username not in user_best_attempts:
            user_best_attempts[username] = attempt
        else:
            current_best = user_best_attempts[username]
            if not hasattr(current_best, 'duration_seconds'):
                current_best.duration_seconds = (current_best.end_time - current_best.start_time).total_seconds()
            
            # Thực hiện so sánh đa tầng tiêu chí:
            if attempt.total_score > current_best.total_score:
                user_best_attempts[username] = attempt
            elif attempt.total_score == current_best.total_score:
                if attempt.duration_seconds < current_best.duration_seconds:
                    user_best_attempts[username] = attempt
                elif attempt.duration_seconds == current_best.duration_seconds:
                    if attempt.start_time < current_best.start_time:
                        user_best_attempts[username] = attempt
                        
    # Sắp xếp toàn bộ danh sách xếp hạng theo 3 tiêu chí:
    leaderboard_list = list(user_best_attempts.values())
    leaderboard_list.sort(key=lambda x: (
        -x.total_score,
        x.duration_seconds,
        x.start_time
    ))
    
    # Định dạng thời gian làm bài thân thiện (ví dụ: 15p 30s)
    for attempt in leaderboard_list:
        total_secs = int(attempt.duration_seconds)
        mins = total_secs // 60
        secs = total_secs % 60
        if mins > 0:
            attempt.formatted_duration = f"{mins}p {secs}s"
        else:
            attempt.formatted_duration = f"{secs}s"

    return render(request, 'lms/leaderboard.html', {
        'test': test,
        'leaderboard': leaderboard_list,
        'mode': mode
    })

@csrf_exempt
@login_required
def upload_image(request):
    if request.method == 'POST' and request.FILES.get('image'):
        image = request.FILES['image']
        # Lưu file vào thư mục media/uploads/
        path = default_storage.save(f'uploads/{image.name}', image)
        url = settings.MEDIA_URL + path
        return JsonResponse({'url': url})
    return JsonResponse({'error': 'Invalid request'}, status=400)

@login_required
def attempt_history(request):
    attempts = Attempt.objects.filter(user=request.user)
    test_id = request.GET.get('test_id')
    test = None
    if test_id:
        test = get_object_or_404(Test, id=test_id)
        attempts = attempts.filter(test=test)
    attempts = attempts.order_by('-start_time')
    return render(request, 'lms/attempt_history.html', {
        'attempts': attempts,
        'test': test
    })

@login_required
def teacher_attempts(request):
    if not request.user.is_staff:
        messages.error(request, "Bạn không có quyền truy cập trang quản trị này.")
        return redirect('course_list')
        
    student_query = request.GET.get('student', '').strip()
    test_query = request.GET.get('test_id', '')
    type_query = request.GET.get('type', 'all')
    
    attempts = Attempt.objects.all().order_by('-start_time')
    
    if student_query:
        attempts = attempts.filter(
            Q(user__username__icontains=student_query) |
            Q(user__first_name__icontains=student_query) |
            Q(user__last_name__icontains=student_query)
        )
        
    if test_query:
        attempts = attempts.filter(test_id=test_query)
        
    if type_query == 'official':
        attempts = attempts.filter(is_official=True)
    elif type_query == 'practice':
        attempts = attempts.filter(is_official=False)
        
    tests = Test.objects.all()
    
    return render(request, 'lms/teacher_attempts.html', {
        'attempts': attempts,
        'tests': tests,
        'selected_test_id': int(test_query) if test_query.isdigit() else '',
        'student_query': student_query,
        'selected_type': type_query
    })

@login_required
def delete_attempt(request, attempt_id):
    if not request.user.is_staff:
        messages.error(request, "Bạn không có quyền thực hiện thao tác này.")
        return redirect('course_list')
        
    attempt = get_object_or_404(Attempt, id=attempt_id)
    test_title = attempt.test.title
    student_name = attempt.user.username
    
    with transaction.atomic():
        attempt.delete()
        
    messages.success(request, f"Đã xóa thành công lượt làm bài của học sinh {student_name} tại đề thi {test_title}.")
    return redirect('teacher_attempts')

@login_required
def admin_dashboard(request):
    if not request.user.is_superuser:
        messages.error(request, "Bạn không có quyền truy cập trang quản trị tối cao.")
        return redirect('course_list')
        
    q = request.GET.get('q', '').strip()
    users = User.objects.all().select_related('profile').order_by('-is_superuser', '-is_staff', 'username')
    
    if q:
        users = users.filter(
            Q(username__icontains=q) |
            Q(email__icontains=q) |
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q)
        )
        
    # Thống kê số lượng
    total_users = User.objects.count()
    total_staff = User.objects.filter(is_staff=True).count()
    question_creators = Profile.objects.filter(can_create_questions=True).count()
    exam_creators = Profile.objects.filter(can_create_exams=True).count()
    course_creators = Profile.objects.filter(can_create_courses=True).count()
    
    # Đảm bảo mỗi user đều có profile
    for u in users:
        if not hasattr(u, 'profile'):
            Profile.objects.create(user=u)
            
    return render(request, 'lms/admin_dashboard.html', {
        'users': users,
        'q': q,
        'total_users': total_users,
        'total_staff': total_staff,
        'question_creators': question_creators,
        'exam_creators': exam_creators,
        'course_creators': course_creators,
    })

@login_required
def toggle_staff_permission(request, user_id, perm_type):
    if not request.user.is_superuser:
        messages.error(request, "Bạn không có quyền thực hiện tác vụ này.")
        return redirect('course_list')
        
    target_user = get_object_or_404(User, id=user_id)
    if target_user.is_superuser:
        messages.error(request, "Không thể thay đổi quyền của tài khoản quản trị tối cao.")
        return redirect('admin_dashboard')
        
    profile, created = Profile.objects.get_or_create(user=target_user)
    
    if perm_type == 'staff':
        target_user.is_staff = not target_user.is_staff
        target_user.save()
        messages.success(request, f"Đã cập nhật trạng thái nhân viên cho {target_user.username}")
    elif perm_type == 'questions':
        profile.can_create_questions = not profile.can_create_questions
        profile.save()
        messages.success(request, f"Đã cập nhật quyền tạo câu hỏi cho {target_user.username}")
    elif perm_type == 'exams':
        profile.can_create_exams = not profile.can_create_exams
        profile.save()
        messages.success(request, f"Đã cập nhật quyền tạo đề thi cho {target_user.username}")
    elif perm_type == 'courses':
        profile.can_create_courses = not profile.can_create_courses
        profile.save()
        messages.success(request, f"Đã cập nhật quyền tạo khóa học & bài học cho {target_user.username}")
        
    # Đồng bộ hóa quyền của Django
    sync_user_django_permissions(target_user)
    
    return redirect('admin_dashboard')

def sync_user_django_permissions(user):
    from django.contrib.auth.models import Permission
    profile = user.profile
    
    # Nếu không phải staff, gỡ bỏ toàn bộ quyền
    if not user.is_staff:
        profile.can_create_questions = False
        profile.can_create_exams = False
        profile.can_create_courses = False
        profile.save()
        user.user_permissions.clear()
        return
        
    codenames = []
    
    if profile.can_create_questions:
        codenames.extend([
            'add_question', 'change_question', 'delete_question', 'view_question',
            'add_choice', 'change_choice', 'delete_choice', 'view_choice',
            'view_questiongroup', 'add_questiongroup', 'change_questiongroup', 'delete_questiongroup'
        ])
        
    if profile.can_create_exams:
        codenames.extend([
            'add_test', 'change_test', 'delete_test', 'view_test',
            'add_testpart', 'change_testpart', 'delete_testpart', 'view_testpart',
            'add_testquestion', 'change_testquestion', 'delete_testquestion', 'view_testquestion',
            'view_testregulation', 'add_testregulation', 'change_testregulation', 'delete_testregulation',
            'view_question', 'view_questiongroup'
        ])
        
    if profile.can_create_courses:
        codenames.extend([
            'add_course', 'change_course', 'delete_course', 'view_course',
            'add_lesson', 'change_lesson', 'delete_lesson', 'view_lesson',
            'add_coursebundle', 'change_coursebundle', 'delete_coursebundle', 'view_coursebundle'
        ])
        
    # Đồng bộ hóa trong Django Auth
    # Đồng bộ hóa trong Django Auth
    perms = Permission.objects.filter(codename__in=codenames, content_type__app_label='lms')
    lms_perms = Permission.objects.filter(content_type__app_label='lms')
    user.user_permissions.remove(*lms_perms)
    user.user_permissions.add(*perms)


@login_required
def admin_transactions(request):
    if not request.user.is_superuser:
        messages.error(request, "Bạn không có quyền truy cập trang quản trị tối cao.")
        return redirect('course_list')
        
    status_filter = request.GET.get('status', 'PENDING').upper()
    valid_statuses = ['ALL', 'PENDING', 'APPROVED', 'REJECTED']
    if status_filter not in valid_statuses:
        status_filter = 'PENDING'
        
    base_transactions = WalletTransaction.objects.filter(
        Q(transaction_type='DEPOSIT') |
        Q(course__isnull=False) |
        Q(course_bundle__isnull=False) |
        Q(test__isnull=False) |
        Q(test_bundle__isnull=False)
    )
    
    transactions = base_transactions.order_by('-created_at')
    
    if status_filter != 'ALL':
        transactions = transactions.filter(status=status_filter)
        
    # Thống kê giao dịch
    pending_count = base_transactions.filter(status='PENDING').count()
    approved_count = base_transactions.filter(status='APPROVED').count()
    rejected_count = base_transactions.filter(status='REJECTED').count()
    
    return render(request, 'lms/admin_transactions.html', {
        'transactions': transactions,
        'status_filter': status_filter,
        'pending_count': pending_count,
        'approved_count': approved_count,
        'rejected_count': rejected_count,
        'total_count': pending_count + approved_count + rejected_count,
    })


@login_required
def approve_transaction_admin(request, tx_id):
    if not request.user.is_superuser:
        messages.error(request, "Bạn không có quyền thực hiện tác vụ này.")
        return redirect('course_list')
        
    if request.method == 'POST':
        tx = get_object_or_404(
            WalletTransaction.objects.filter(
                Q(transaction_type='DEPOSIT') |
                Q(course__isnull=False) |
                Q(course_bundle__isnull=False) |
                Q(test__isnull=False) |
                Q(test_bundle__isnull=False)
            ),
            id=tx_id
        )
        if tx.status != 'PENDING':
            messages.error(request, "Giao dịch này đã được xử lý từ trước.")
            return redirect('admin_transactions')
            
        tx.approve()
            
        formatted_amount = f"{int(tx.amount):,}"
        if tx.transaction_type == 'DEPOSIT':
            messages.success(request, f"Đã phê duyệt nạp {formatted_amount}đ cho tài khoản {tx.user.username} thành công.")
        else:
            messages.success(request, f"Đã phê duyệt đơn thanh toán mua sản phẩm #{tx.id} của {tx.user.username} thành công.")
        
    return redirect('admin_transactions')


@login_required
def reject_transaction_admin(request, tx_id):
    if not request.user.is_superuser:
        messages.error(request, "Bạn không có quyền thực hiện tác vụ này.")
        return redirect('course_list')
        
    if request.method == 'POST':
        tx = get_object_or_404(
            WalletTransaction.objects.filter(
                Q(transaction_type='DEPOSIT') |
                Q(course__isnull=False) |
                Q(course_bundle__isnull=False) |
                Q(test__isnull=False) |
                Q(test_bundle__isnull=False)
            ),
            id=tx_id
        )
        if tx.status != 'PENDING':
            messages.error(request, "Giao dịch này đã được xử lý từ trước.")
            return redirect('admin_transactions')
            
        tx.status = 'REJECTED'
        tx.save()
        
        messages.success(request, f"Đã từ chối giao dịch #{tx.id} của {tx.user.username}.")
        
    return redirect('admin_transactions')


@login_required
def create_question(request):
    """View tạo câu hỏi mới trên giao diện Frontend (hỗ trợ Live LaTeX/Markdown preview)."""
    if not request.user.is_superuser and not request.user.profile.can_create_questions:
        messages.error(request, "Bạn không có quyền tạo câu hỏi.")
        return redirect('course_list')
        
    if request.method == 'POST':
        content = request.POST.get('content', '').strip()
        question_type = int(request.POST.get('question_type', 1))
        group_id = request.POST.get('group_id', '')
        equivalent_group_id = request.POST.get('equivalent_group_id', '')
        is_public = request.POST.get('is_public') == 'on'
        
        group = None
        if group_id:
            group = get_object_or_404(QuestionGroup, id=group_id)
            
        equivalent_group = None
        if equivalent_group_id:
            equivalent_group = get_object_or_404(EquivalentQuestionGroup, id=equivalent_group_id)
            
        with transaction.atomic():
            question = Question.objects.create(
                content=content,
                question_type=question_type,
                group=group,
                equivalent_group=equivalent_group,
                is_public=is_public,
                creator=request.user
            )
            
            # Lưu đáp án dựa trên loại câu hỏi
            if question_type == 1:
                # Trắc nghiệm 1 lựa chọn
                choice_texts = request.POST.getlist('choice_text_1[]')
                choice_positions = request.POST.getlist('choice_position_1[]')
                correct_idx = int(request.POST.get('correct_choice_1', 0))
                for idx, txt in enumerate(choice_texts):
                    if txt.strip():
                        pos = int(choice_positions[idx]) if idx < len(choice_positions) and choice_positions[idx].strip() else 1
                        Choice.objects.create(
                            question=question,
                            content=txt.strip(),
                            is_correct=(idx == correct_idx),
                            position=pos
                        )
            elif question_type == 2:
                # Trắc nghiệm nhiều lựa chọn
                choice_texts = request.POST.getlist('choice_text_2[]')
                choice_positions = request.POST.getlist('choice_position_2[]')
                correct_indices = [int(i) for i in request.POST.getlist('correct_choices_2[]')]
                for idx, txt in enumerate(choice_texts):
                    if txt.strip():
                        pos = int(choice_positions[idx]) if idx < len(choice_positions) and choice_positions[idx].strip() else 1
                        Choice.objects.create(
                            question=question,
                            content=txt.strip(),
                            is_correct=(idx in correct_indices),
                            position=pos
                        )
            elif question_type == 3:
                # Đúng/Sai 4 ý
                choice_texts = request.POST.getlist('choice_text_3[]')
                choice_positions = request.POST.getlist('choice_position_3[]')
                for idx in range(4):
                    txt = choice_texts[idx] if idx < len(choice_texts) else ""
                    ans_val = request.POST.get(f'tf_choice_3_{idx}', 'false') == 'true'
                    if txt.strip():
                        pos = int(choice_positions[idx]) if idx < len(choice_positions) and choice_positions[idx].strip() else 1
                        Choice.objects.create(
                            question=question,
                            content=txt.strip(),
                            is_correct=ans_val,
                            position=pos
                        )
            # Lưu trả lời ngắn (chấp nhận nhiều đáp án đúng, mỗi đáp án có cấu hình so khớp riêng biệt)
            elif question_type == 4:
                choice_texts = request.POST.getlist('choice_text_4[]')
                case_sensitive_list = request.POST.getlist('is_case_sensitive_4[]')
                ignore_spaces_list = request.POST.getlist('ignore_spaces_4[]')
                
                for idx, txt in enumerate(choice_texts):
                    if txt.strip():
                        is_cs = (case_sensitive_list[idx] == 'on') if idx < len(case_sensitive_list) else False
                        is_is = (ignore_spaces_list[idx] == 'on') if idx < len(ignore_spaces_list) else True
                        
                        Choice.objects.create(
                            question=question,
                            content=txt.strip(),
                            is_correct=is_cs,              # Lưu Phân biệt chữ hoa/thường vào is_correct
                            position=(1 if is_is else 0)   # Lưu Bỏ qua khoảng trắng vào position (1: có, 0: không)
                        )
                        
            # Link to test if test_id is present
            test_id = request.GET.get('test_id') or request.POST.get('test_id')
            test = None
            if test_id:
                test = Test.objects.filter(id=test_id).first()
                if test:
                    # Ensure part 1 has instruction
                    TestPartInstruction.objects.get_or_create(test=test, part_number=1)
                    # Calculate next order_index
                    from django.db.models import Max
                    current_max = TestQuestion.objects.filter(test=test, part_number=1).aggregate(Max('order_index'))['order_index__max'] or 0
                    next_order = current_max + 1
                    
                    # Create test question link
                    TestQuestion.objects.create(
                        test=test,
                        question=question,
                        points=1.0,
                        optional_type='NONE',
                        order_index=next_order,
                        part_number=1
                    )
                        
        messages.success(request, "Tạo câu hỏi mới thành công!")
        if request.POST.get('action') == 'save_and_continue':
            url = reverse('create_question')
            params = []
            if group_id:
                params.append(f"group_id={group_id}")
            if test_id:
                params.append(f"test_id={test_id}")
            if params:
                url += "?" + "&".join(params)
            return redirect(url)
            
        if test:
            return redirect('manage_test_structure', test_id=test.id)
        return redirect('/admin/lms/question/')
        
    groups = QuestionGroup.objects.all()
    equivalent_groups = EquivalentQuestionGroup.objects.all()
    preselected_group_id = request.GET.get('group_id', '')
    test_id = request.GET.get('test_id', '')
    test = None
    if test_id:
        test = Test.objects.filter(id=test_id).first()
    return render(request, 'lms/create_question.html', {
        'groups': groups,
        'equivalent_groups': equivalent_groups,
        'preselected_group_id': preselected_group_id,
        'test': test,
        'test_id': test_id,
    })


@login_required
def edit_question(request, pk):
    """View chỉnh sửa câu hỏi trên giao diện Frontend (hỗ trợ Live LaTeX/Markdown preview)."""
    question = get_object_or_404(Question, pk=pk)
    
    # Chỉ cho phép người tạo hoặc superuser chỉnh sửa câu hỏi (Cô lập Option A)
    if not request.user.is_superuser and question.creator != request.user:
        messages.error(request, "Bạn chỉ có thể chỉnh sửa câu hỏi do chính mình tạo ra.")
        return redirect('/admin/lms/question/')
        
    # Quyền soạn thảo nói chung
    if not request.user.is_superuser and not request.user.profile.can_create_questions:
        messages.error(request, "Bạn không có quyền chỉnh sửa câu hỏi.")
        return redirect('course_list')
        
    if request.method == 'POST':
        content = request.POST.get('content', '').strip()
        question_type = int(request.POST.get('question_type', 1))
        group_id = request.POST.get('group_id', '')
        equivalent_group_id = request.POST.get('equivalent_group_id', '')
        is_public = request.POST.get('is_public') == 'on'
        
        group = None
        if group_id:
            group = get_object_or_404(QuestionGroup, id=group_id)
            
        equivalent_group = None
        if equivalent_group_id:
            equivalent_group = get_object_or_404(EquivalentQuestionGroup, id=equivalent_group_id)
            
        with transaction.atomic():
            question.content = content
            question.question_type = question_type
            question.group = group
            question.equivalent_group = equivalent_group
            question.is_public = is_public
            question.save()
            
            # Xóa các Choice cũ của câu hỏi
            question.choices.all().delete()
            
            # Lưu đáp án dựa trên loại câu hỏi
            if question_type == 1:
                # Trắc nghiệm 1 lựa chọn
                choice_texts = request.POST.getlist('choice_text_1[]')
                choice_positions = request.POST.getlist('choice_position_1[]')
                correct_idx = int(request.POST.get('correct_choice_1', 0))
                for idx, txt in enumerate(choice_texts):
                    if txt.strip():
                        pos = int(choice_positions[idx]) if idx < len(choice_positions) and choice_positions[idx].strip() else 1
                        Choice.objects.create(
                            question=question,
                            content=txt.strip(),
                            is_correct=(idx == correct_idx),
                            position=pos
                        )
            elif question_type == 2:
                # Trắc nghiệm nhiều lựa chọn
                choice_texts = request.POST.getlist('choice_text_2[]')
                choice_positions = request.POST.getlist('choice_position_2[]')
                correct_indices = [int(i) for i in request.POST.getlist('correct_choices_2[]')]
                for idx, txt in enumerate(choice_texts):
                    if txt.strip():
                        pos = int(choice_positions[idx]) if idx < len(choice_positions) and choice_positions[idx].strip() else 1
                        Choice.objects.create(
                            question=question,
                            content=txt.strip(),
                            is_correct=(idx in correct_indices),
                            position=pos
                        )
            elif question_type == 3:
                # Đúng/Sai 4 ý
                choice_texts = request.POST.getlist('choice_text_3[]')
                choice_positions = request.POST.getlist('choice_position_3[]')
                for idx in range(4):
                    txt = choice_texts[idx] if idx < len(choice_texts) else ""
                    ans_val = request.POST.get(f'tf_choice_3_{idx}', 'false') == 'true'
                    if txt.strip():
                        pos = int(choice_positions[idx]) if idx < len(choice_positions) and choice_positions[idx].strip() else 1
                        Choice.objects.create(
                            question=question,
                            content=txt.strip(),
                            is_correct=ans_val,
                            position=pos
                        )
            elif question_type == 4:
                # Trả lời ngắn
                choice_texts = request.POST.getlist('choice_text_4[]')
                case_sensitive_list = request.POST.getlist('is_case_sensitive_4[]')
                ignore_spaces_list = request.POST.getlist('ignore_spaces_4[]')
                
                for idx, txt in enumerate(choice_texts):
                    if txt.strip():
                        is_cs = (case_sensitive_list[idx] == 'on') if idx < len(case_sensitive_list) else False
                        is_is = (ignore_spaces_list[idx] == 'on') if idx < len(ignore_spaces_list) else True
                        
                        Choice.objects.create(
                            question=question,
                            content=txt.strip(),
                            is_correct=is_cs,              # Lưu Phân biệt chữ hoa/thường vào is_correct
                            position=(1 if is_is else 0)   # Lưu Bỏ qua khoảng trắng vào position (1: có, 0: không)
                        )
                        
        messages.success(request, "Cập nhật câu hỏi thành công!")
        if request.POST.get('action') == 'save_and_continue':
            return redirect('edit_question', pk=question.id)
        return redirect('preview_question', pk=question.id)
        
    # Đọc và định dạng dữ liệu cho giao diện Alpine.js
    import json
    
    # Defaults
    c1 = [
        { 'text': '$\\Delta = b^2 - 4ac$', 'is_correct': True },
        { 'text': '$\\Delta = b^2 + 4ac$', 'is_correct': False },
        { 'text': '$\\Delta = b - 4ac$', 'is_correct': False },
        { 'text': '$\\Delta = b^2 - ac$', 'is_correct': False }
    ]
    c2 = [
        { 'text': 'Phương trình có 2 nghiệm phân biệt khi $\\Delta > 0$', 'is_correct': True },
        { 'text': 'Phương trình có nghiệm kép khi $\\Delta = 0$', 'is_correct': True },
        { 'text': 'Phương trình vô nghiệm khi $\\Delta < 0$', 'is_correct': False }
    ]
    c3 = [
        { 'text': 'Nếu $a$ và $c$ trái dấu thì phương trình luôn có 2 nghiệm phân biệt.', 'is_correct': True },
        { 'text': 'Khi $\\Delta = 0$, nghiệm kép của phương trình là $x = -b/a$.', 'is_correct': False },
        { 'text': 'Biệt thức $\\Delta$ xác định tính chất nghiệm của phương trình bậc hai.', 'is_correct': True },
        { 'text': 'Nếu $\\Delta < 0$, phương trình có nghiệm kép.', 'is_correct': False }
    ]
    c4 = [
        { 'text': 'b^2-4ac', 'is_case_sensitive': False, 'ignore_spaces': True },
        { 'text': 'delta = b^2 - 4ac', 'is_case_sensitive': False, 'ignore_spaces': True }
    ]
    
    choices = list(question.choices.all().order_by('id'))
    correct_choice_1 = 0
    
    if question.question_type == 1 and choices:
        c1 = []
        for idx, opt in enumerate(choices):
            c1.append({'text': opt.content, 'is_correct': opt.is_correct, 'position': opt.position})
            if opt.is_correct:
                correct_choice_1 = idx
    elif question.question_type == 2 and choices:
        c2 = [{'text': opt.content, 'is_correct': opt.is_correct, 'position': opt.position} for opt in choices]
    elif question.question_type == 3 and choices:
        # Đảm bảo đủ 4 ý
        c3 = []
        for idx in range(4):
            if idx < len(choices):
                opt = choices[idx]
                c3.append({'text': opt.content, 'is_correct': opt.is_correct, 'position': opt.position})
            else:
                c3.append({'text': '', 'is_correct': False, 'position': 1})
    elif question.question_type == 4 and choices:
        c4 = [{'text': opt.content, 'is_case_sensitive': opt.is_correct, 'ignore_spaces': (opt.position == 1)} for opt in choices]
        
    groups = QuestionGroup.objects.all()
    equivalent_groups = EquivalentQuestionGroup.objects.all()
    
    preloaded_data = {
        'question_type': question.question_type,
        'content': question.content,
        'group_id': str(question.group.id) if question.group else '',
        'equivalent_group_id': str(question.equivalent_group.id) if question.equivalent_group else '',
        'is_public': question.is_public,
        'correct_choice_1': correct_choice_1,
        'choices1': c1,
        'choices2': c2,
        'choices3': c3,
        'choices4': c4,
    }
    
    return render(request, 'lms/edit_question.html', {
        'question': question,
        'groups': groups,
        'equivalent_groups': equivalent_groups,
        'preloaded_json': json.dumps(preloaded_data),
    })

@login_required
def question_list(request):
    if not (request.user.is_staff or request.user.is_superuser):
        messages.error(request, "Bạn không có quyền truy cập Ngân hàng Câu hỏi.")
        return redirect('course_list')
        
    from django.core.paginator import Paginator
    import json

    q_search = request.GET.get('q', '').strip()
    q_type = request.GET.get('type', '').strip()
    q_group = request.GET.get('group', '').strip()
    q_visibility = request.GET.get('visibility', 'all').strip()
    q_creator = request.GET.get('creator', 'all').strip()
    q_sort = request.GET.get('sort', 'newest').strip()

    questions = Question.objects.select_related('group', 'creator').prefetch_related('choices')

    # 1. Tìm kiếm theo văn bản câu hỏi
    if q_search:
        questions = questions.filter(content__icontains=q_search)

    # 2. Lọc theo loại câu hỏi (Hình thức)
    if q_type:
        try:
            questions = questions.filter(question_type=int(q_type))
        except ValueError:
            pass

    # 3. Lọc theo nhóm câu hỏi
    if q_group:
        try:
            questions = questions.filter(group_id=int(q_group))
        except ValueError:
            pass

    # 4. Lọc theo độ công khai (is_public)
    if q_visibility == 'public':
        questions = questions.filter(is_public=True)
    elif q_visibility == 'private':
        questions = questions.filter(is_public=False)

    # 5. Lọc theo tác giả câu hỏi
    if q_creator == 'mine':
        questions = questions.filter(creator=request.user)
    elif q_creator == 'others':
        questions = questions.exclude(creator=request.user)

    # 6. Sắp xếp kết quả
    if q_sort == 'oldest':
        questions = questions.order_by('id')
    elif q_sort == 'group':
        questions = questions.order_by('group__name', '-id')
    else: # newest
        questions = questions.order_by('-id')

    # Phân trang (15 câu hỏi / trang)
    paginator = Paginator(questions, 15)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    groups = QuestionGroup.objects.all()

    # Thêm cờ có thể sửa / xóa cho mỗi câu hỏi
    for q in page_obj:
        q.can_edit = (q.creator == request.user or request.user.is_superuser)

    return render(request, 'lms/question_list.html', {
        'page_obj': page_obj,
        'groups': groups,
        'q_search': q_search,
        'q_type': q_type,
        'q_group': q_group,
        'q_visibility': q_visibility,
        'q_creator': q_creator,
        'q_sort': q_sort,
    })

@login_required
def delete_question(request, pk):
    if not (request.user.is_staff or request.user.is_superuser):
        messages.error(request, "Bạn không có quyền thực hiện chức năng này.")
        return redirect('course_list')

    question = get_object_or_404(Question, pk=pk)

    # Kiểm tra quyền sở hữu (Creator hoặc Superuser)
    if not (question.creator == request.user or request.user.is_superuser):
        messages.error(request, "Bạn chỉ có thể xoá câu hỏi do chính mình tạo ra.")
        return redirect('question_list')

    # Thực hiện xoá nguyên tử
    with transaction.atomic():
        question.delete()

    messages.success(request, f"Đã xoá thành công câu hỏi #{pk} khỏi ngân hàng câu hỏi.")
    return redirect('question_list')

def rate_limit(key_prefix, limit, period):
    """
    Decorator giới hạn tần suất gửi yêu cầu sử dụng Django Cache.
    limit: Số lượng request tối đa trong khoảng thời gian.
    period: Khoảng thời gian tính bằng giây.
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if request.user.is_authenticated:
                identifier = f"user_{request.user.id}"
            else:
                identifier = f"ip_{request.META.get('REMOTE_ADDR')}"
                
            cache_key = f"ratelimit:{key_prefix}:{identifier}"
            request_times = cache.get(cache_key, [])
            now = time.time()
            
            # Loại bỏ các request đã quá hạn (period)
            request_times = [t for t in request_times if now - t < period]
            
            if len(request_times) >= limit:
                if request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.content_type:
                    return JsonResponse({
                        'error': 'Bạn đã gửi quá nhiều yêu cầu luyện tập. Vui lòng thử lại sau ít phút.'
                    }, status=429)
                raise Http404("Quá nhiều yêu cầu. Vui lòng thử lại sau.")
                
            request_times.append(now)
            cache.set(cache_key, request_times, period)
            
            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator

@rate_limit(key_prefix='q_detail', limit=30, period=60)
def question_detail(request, pk):
    # Tìm kiếm câu hỏi
    question = get_object_or_404(Question.objects.select_related('group', 'creator').prefetch_related('choices'), pk=pk)

    # Kiểm tra phân quyền truy cập
    is_privileged = request.user.is_authenticated and (request.user.is_staff or request.user.is_superuser)
    if not is_privileged:
        # Học sinh hoặc Khách chưa đăng nhập chỉ xem được câu hỏi Public
        if not question.is_public:
            messages.error(request, "Câu hỏi này là nội bộ, bạn không có quyền xem.")
            return redirect('course_list')

    import random
    seed = random.randint(1, 10000000)
    request.session[f'q_seed_{question.id}'] = seed

    from lms.bbcode_parser import BBBienParser
    parser = BBBienParser(seed=seed)
    
    question.content = parser.parse(question.content)
    
    choices = list(question.choices.all())
    for c in choices:
        c.content = parser.parse(c.content)

    return render(request, 'lms/question_detail.html', {
        'question': question,
        'choices': choices,
    })

@login_required
def duplicate_question(request, pk):
    # Tìm kiếm câu hỏi gốc
    original_question = get_object_or_404(Question, pk=pk)
    
    # Kiểm tra phân quyền tạo câu hỏi
    if not request.user.is_superuser and not request.user.profile.can_create_questions:
        messages.error(request, "Bạn không có quyền tạo/nhân đôi câu hỏi.")
        return redirect('course_list')
        
    if request.method != 'POST':
        messages.error(request, "Phương thức yêu cầu không hợp lệ.")
        return redirect('question_detail', pk=pk)
        
    # Tiến hành nhân đôi trong transaction
    with transaction.atomic():
        new_question = Question.objects.create(
            group=original_question.group,
            content=original_question.content,
            question_type=original_question.question_type,
            is_public=original_question.is_public,
            creator=request.user
        )
        
        # Sao chép các đáp án (Choice)
        for choice in original_question.choices.all():
            Choice.objects.create(
                question=new_question,
                content=choice.content,
                is_correct=choice.is_correct,
                position=choice.position
            )
            
        # Xử lý nhóm tương đương
        if request.POST.get('create_equivalent') == 'on':
            if original_question.equivalent_group:
                # Đã có nhóm tương đương -> Thêm câu hỏi mới vào nhóm đó
                new_question.equivalent_group = original_question.equivalent_group
                new_question.save()
            else:
                # Chưa có nhóm tương đương -> Tạo mới nhóm tương đương và thêm cả 2 vào
                eq_group_name = f"Bài gốc {original_question.id}"
                eq_group = EquivalentQuestionGroup.objects.create(name=eq_group_name)
                
                original_question.equivalent_group = eq_group
                original_question.save()
                
                new_question.equivalent_group = eq_group
                new_question.save()
                
    messages.success(request, f"Nhân đôi câu hỏi #{original_question.id} thành công! Bạn hiện đang chỉnh sửa bản sao mới tạo #{new_question.id}.")
    return redirect('edit_question', pk=new_question.id)

@login_required
def preview_question(request, pk):
    """Giao diện xem thử câu hỏi chi tiết và đáp án đúng dành cho Giáo viên / Admin."""
    if not request.user.is_superuser and not request.user.profile.can_create_questions:
        messages.error(request, "Bạn không có quyền xem thử câu hỏi này.")
        return redirect('course_list')
        
    question = get_object_or_404(Question.objects.select_related('group', 'creator', 'equivalent_group').prefetch_related('choices'), pk=pk)
    
    equivalent_questions = []
    if question.equivalent_group:
        equivalent_questions = Question.objects.filter(equivalent_group=question.equivalent_group).exclude(pk=question.pk).prefetch_related('choices')
    
    return render(request, 'lms/preview_question.html', {
        'question': question,
        'choices': question.choices.all(),
        'equivalent_questions': equivalent_questions,
    })

@rate_limit(key_prefix='q_check', limit=15, period=60)
@csrf_exempt
def check_question_answer(request, pk):
    # Chỉ cho phép kiểm tra nếu người dùng đã đăng nhập
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Vui lòng đăng nhập để kiểm tra đáp án.'}, status=401)

    question = get_object_or_404(Question, pk=pk)
    
    # Kiểm tra quyền truy cập
    is_privileged = request.user.is_staff or request.user.is_superuser
    if not is_privileged and not question.is_public:
        return JsonResponse({'error': 'Bạn không có quyền thực hành câu hỏi này.'}, status=403)

    import json
    # Đọc dữ liệu
    if request.content_type == 'application/json':
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            data = {}
    else:
        data = request.POST

    seed = request.session.get(f'q_seed_{question.id}', question.id)
    from lms.bbcode_parser import BBBienParser
    parser = BBBienParser(seed=seed)
    
    choices = list(question.choices.all())
    for c in choices:
        c.content = parser.parse(c.content)

    is_correct = False
    results = {}
    correct_answers = []

    # 1. Trắc nghiệm 1 lựa chọn
    if question.question_type == 1:
        selected_choice_id = data.get('choice_id')
        if selected_choice_id:
            try:
                selected_choice = next((c for c in choices if c.id == int(selected_choice_id)), None)
                if selected_choice:
                    is_correct = selected_choice.is_correct
            except ValueError:
                is_correct = False
        
        correct_choices = [c for c in choices if c.is_correct]
        correct_choice = correct_choices[0] if correct_choices else None
        if correct_choice:
            correct_answers.append(correct_choice.content)
            
    # 2. Trắc nghiệm nhiều lựa chọn
    elif question.question_type == 2:
        selected_choice_ids = data.get('choice_ids', [])
        if isinstance(selected_choice_ids, str):
            try:
                selected_choice_ids = json.loads(selected_choice_ids)
            except json.JSONDecodeError:
                selected_choice_ids = [int(x) for x in selected_choice_ids.split(',') if x]
        
        selected_choice_ids = [int(cid) for cid in selected_choice_ids if cid]
        correct_choice_ids = [c.id for c in choices if c.is_correct]
        is_correct = (set(selected_choice_ids) == set(correct_choice_ids))
        
        for c in choices:
            if c.is_correct:
                correct_answers.append(c.content)

    # 3. Đúng/Sai độc lập
    elif question.question_type == 3:
        user_selections = data.get('answers', {})
        if isinstance(user_selections, str):
            try:
                user_selections = json.loads(user_selections)
            except json.JSONDecodeError:
                user_selections = {}
                
        correct_count = 0
        total_choices = len(choices)
        
        for c in choices:
            user_val = user_selections.get(str(c.id))
            if user_val is None:
                user_val = user_selections.get(c.id)
                
            if isinstance(user_val, str):
                user_val = (user_val.lower() == 'true')
                
            is_match = (user_val == c.is_correct)
            results[c.id] = {
                'is_match': is_match,
            }
            if is_match:
                correct_count += 1
                
            correct_answers.append(f"{c.content}: {'Đúng' if c.is_correct else 'Sai'}")
            
        is_correct = (correct_count == total_choices)

    # 4. Trả lời ngắn
    elif question.question_type == 4:
        user_answer = data.get('answer_text', '').strip()
        
        matched_choice = None
        for choice in choices:
            is_case_sensitive = choice.is_correct
            ignore_spaces = (choice.position == 1)
            
            if ignore_spaces:
                user_ans_clean = "".join(user_answer.split())
                choice_ans_clean = "".join(choice.content.split())
            else:
                user_ans_clean = user_answer.strip()
                choice_ans_clean = choice.content.strip()
                
            if not is_case_sensitive:
                user_ans_clean = user_ans_clean.lower()
                choice_ans_clean = choice_ans_clean.lower()
                
            if user_ans_clean == choice_ans_clean:
                matched_choice = choice
                break
                
        is_correct = (matched_choice is not None)
        
        for c in choices:
            correct_answers.append(c.content)

    response_data = {
        'is_correct': is_correct,
        'results': results,
    }
    if is_privileged:
        response_data['correct_answers'] = correct_answers

    return JsonResponse(response_data)


@login_required
@csrf_exempt
def create_group_ajax(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({'error': 'Bạn không có quyền thực hiện tác vụ này.'}, status=403)
    if request.method == 'POST':
        import json
        try:
            data = json.loads(request.body)
            name = data.get('name', '').strip()
            if not name:
                return JsonResponse({'error': 'Tên nhóm không được để trống.'}, status=400)
            
            group = QuestionGroup.objects.create(name=name)
            return JsonResponse({'id': group.id, 'name': group.name})
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'error': 'Phương thức không hợp lệ.'}, status=405)


@login_required
@csrf_exempt
def bulk_assign_group_ajax(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({'error': 'Bạn không có quyền thực hiện tác vụ này.'}, status=403)
    if request.method == 'POST':
        import json
        try:
            data = json.loads(request.body)
            question_ids = data.get('question_ids', [])
            group_id = data.get('group_id')
            
            group = None
            if group_id:
                group = get_object_or_404(QuestionGroup, id=int(group_id))
                
            with transaction.atomic():
                target_qs = Question.objects.filter(id__in=question_ids)
                if not request.user.is_superuser:
                    target_qs = target_qs.filter(creator=request.user)
                count = target_qs.update(group=group)
                
            return JsonResponse({'success': True, 'count': count})
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'error': 'Phương thức không hợp lệ.'}, status=405)


@login_required
def duplicate_test(request, test_id):
    original_test = get_object_or_404(Test, id=test_id)
    
    if not request.user.is_superuser and not request.user.is_staff and not getattr(request.user.profile, 'can_create_exams', False) and not getattr(request.user.profile, 'can_create_courses', False):
        messages.error(request, "Bạn không có quyền tạo/nhân đôi đề thi.")
        return redirect('test_list')
        
    if not request.user.is_superuser and original_test.creator != request.user:
        messages.error(request, "Bạn không có quyền nhân đôi đề thi này.")
        return redirect('test_list')
        
    if request.method != 'POST':
        messages.error(request, "Phương thức yêu cầu không hợp lệ.")
        return redirect('test_list')
        
    with transaction.atomic():
        new_test = Test.objects.create(
            title=f"[Bản sao] {original_test.title}",
            short_description=original_test.short_description,
            price=original_test.price,
            duration=original_test.duration,
            allow_practice=original_test.allow_practice,
            is_official=original_test.is_official,
            shuffle_parts=original_test.shuffle_parts,
            regulation=original_test.regulation,
            start_time=original_test.start_time,
            end_time=original_test.end_time,
            creator=request.user
        )
        
        for pi in original_test.part_instructions.all():
            TestPartInstruction.objects.create(
                test=new_test,
                part_number=pi.part_number,
                instruction=pi.instruction
            )
            
        for tq in original_test.questions.all():
            TestQuestion.objects.create(
                test=new_test,
                question=tq.question,
                points=tq.points,
                optional_type=tq.optional_type,
                order_index=tq.order_index,
                part_number=tq.part_number
            )
            
    messages.success(request, f"Nhân đôi đề thi '{original_test.title}' thành công! Bản sao mới: '{new_test.title}'.")
    return redirect('manage_test_structure', test_id=new_test.id)


@login_required
def create_test(request):
    """Bước 1: Thiết lập cấu hình đề thi thô mới."""
    if not request.user.is_superuser and not request.user.is_staff and not getattr(request.user.profile, 'can_create_exams', False) and not getattr(request.user.profile, 'can_create_courses', False):
        messages.error(request, "Bạn không có quyền tạo đề thi.")
        return redirect('test_list')
        
    regulations = TestRegulation.objects.all()
    
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        short_description = request.POST.get('short_description', '').strip()
        price = request.POST.get('price', '0')
        duration = request.POST.get('duration', '60')
        allow_practice = request.POST.get('allow_practice') == 'on'
        is_official = request.POST.get('is_official') == 'on'
        shuffle_parts = request.POST.get('shuffle_parts') == 'on'
        regulation_id = request.POST.get('regulation_id')
        test_type = request.POST.get('test_type', 'STANDALONE').strip()
        
        start_time_str = request.POST.get('start_time', '').strip()
        end_time_str = request.POST.get('end_time', '').strip()
        
        start_time = None
        end_time = None
        
        if start_time_str:
            try:
                start_time = timezone.datetime.fromisoformat(start_time_str)
            except Exception:
                start_time = None
        if end_time_str:
            try:
                end_time = timezone.datetime.fromisoformat(end_time_str)
            except Exception:
                end_time = None
            
        regulation = None
        if regulation_id:
            regulation = get_object_or_404(TestRegulation, id=regulation_id)
            
        if not title:
            messages.error(request, "Tiêu đề đề thi không được để trống.")
            return render(request, 'lms/create_test.html', {'regulations': regulations})
            
        try:
            test = Test.objects.create(
                title=title,
                short_description=short_description,
                price=float(price),
                duration=int(duration),
                allow_practice=allow_practice,
                is_official=is_official,
                shuffle_parts=shuffle_parts,
                test_type=test_type,
                regulation=regulation,
                start_time=start_time,
                end_time=end_time,
                creator=request.user
            )
            messages.success(request, f"Tạo đề thi '{title}' thành công. Hãy thiết lập cấu trúc đề thi tại đây!")
            return redirect('manage_test_structure', test_id=test.id)
        except Exception as e:
            messages.error(request, f"Đã xảy ra lỗi khi tạo đề thi: {str(e)}")
            return render(request, 'lms/create_test.html', {'regulations': regulations})
            
    return render(request, 'lms/create_test.html', {'regulations': regulations})


@login_required
def edit_test_basic(request, test_id):
    """Sửa đổi các thông số cấu hình cơ bản của Đề thi."""
    test = get_object_or_404(Test, id=test_id)
    if not request.user.is_superuser and test.creator != request.user:
        messages.error(request, "Bạn không có quyền sửa đề thi này.")
        return redirect('test_list')
        
    regulations = TestRegulation.objects.all()
    
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        short_description = request.POST.get('short_description', '').strip()
        price = request.POST.get('price', '0')
        duration = request.POST.get('duration', '60')
        allow_practice = request.POST.get('allow_practice') == 'on'
        is_official = request.POST.get('is_official') == 'on'
        shuffle_parts = request.POST.get('shuffle_parts') == 'on'
        regulation_id = request.POST.get('regulation_id')
        test_type = request.POST.get('test_type', 'STANDALONE').strip()
        
        start_time_str = request.POST.get('start_time', '').strip()
        end_time_str = request.POST.get('end_time', '').strip()
        
        start_time = None
        end_time = None
        
        if start_time_str:
            try:
                start_time = timezone.datetime.fromisoformat(start_time_str)
            except Exception:
                start_time = None
        if end_time_str:
            try:
                end_time = timezone.datetime.fromisoformat(end_time_str)
            except Exception:
                end_time = None
            
        regulation = None
        if regulation_id:
            regulation = get_object_or_404(TestRegulation, id=regulation_id)
            
        if not title:
            messages.error(request, "Tiêu đề đề thi không được để trống.")
            return render(request, 'lms/create_test.html', {'test': test, 'regulations': regulations, 'is_edit': True})
            
        try:
            test.title = title
            test.short_description = short_description
            test.price = float(price)
            test.duration = int(duration)
            test.allow_practice = allow_practice
            test.is_official = is_official
            test.shuffle_parts = shuffle_parts
            test.test_type = test_type
            test.regulation = regulation
            test.start_time = start_time
            test.end_time = end_time
            test.save()
            
            messages.success(request, f"Cập nhật cấu hình cơ bản cho đề thi '{title}' thành công!")
            return redirect('manage_test_structure', test_id=test.id)
        except Exception as e:
            messages.error(request, f"Đã xảy ra lỗi khi cập nhật đề thi: {str(e)}")
            return render(request, 'lms/create_test.html', {'test': test, 'regulations': regulations, 'is_edit': True})
            
    return render(request, 'lms/create_test.html', {'test': test, 'regulations': regulations, 'is_edit': True})


@login_required
def manage_test_structure(request, test_id):
    """Bước 2: Giao diện quản lý cấu trúc đề thi thời gian thực phân bổ hai cột."""
    test = get_object_or_404(Test, id=test_id)
    if not request.user.is_superuser and test.creator != request.user:
        messages.error(request, "Bạn không có quyền quản lý cấu trúc đề thi này.")
        return redirect('test_list')
        
    # Đảm bảo có các TestPartInstruction cho tất cả part_number đã tồn tại trong TestQuestion
    question_part_nums = set(TestQuestion.objects.filter(test=test).values_list('part_number', flat=True))
    instruction_part_nums = set(TestPartInstruction.objects.filter(test=test).values_list('part_number', flat=True))
    part_numbers = sorted(list(question_part_nums | instruction_part_nums))
    
    # Đảm bảo có ít nhất 1 phần thi mặc định (Phần 1) nếu đề thi trống rỗng
    if not part_numbers:
        part_numbers = [1]
        
    # Tự động đồng bộ hóa tạo các bản ghi TestPartInstruction nếu có part_number nhưng chưa có bản ghi instruction
    for part_num in part_numbers:
        TestPartInstruction.objects.get_or_create(test=test, part_number=part_num)
        
    # Truy vấn lại sau khi đã đảm bảo đồng bộ
    part_instructions = {
        pi.part_number: pi
        for pi in TestPartInstruction.objects.filter(test=test).select_related('instruction')
    }
    
    test_questions = TestQuestion.objects.filter(test=test).select_related('question').order_by('order_index')
    questions_by_part = {}
    for tq in test_questions:
        questions_by_part.setdefault(tq.part_number, []).append(tq)
        
    parts_data = []
    for part_num in sorted(part_instructions.keys()):
        pi = part_instructions.get(part_num)
        instruction_text = pi.instruction.content if (pi and pi.instruction) else ""
        tqs = questions_by_part.get(part_num, [])
        
        parts_data.append({
            'id': pi.id,  # Đây là ID duy nhất của TestPartInstruction trong Database!
            'part_number': part_num,
            'title_roman': int_to_roman(part_num),
            'instruction': instruction_text,
            'questions': tqs,
        })
        
    groups = QuestionGroup.objects.all()
    shared_instructions = SharedInstruction.objects.all().order_by('title')
    return render(request, 'lms/manage_test_structure.html', {
        'test': test,
        'parts_data': parts_data,
        'groups': groups,
        'shared_instructions': shared_instructions
    })


@login_required
def preview_test(request, test_id):
    """Giao diện xem thử đề thi (không đảo câu hỏi/đáp án) và hiển thị đáp án đúng dành cho Giáo viên."""
    test = get_object_or_404(Test, id=test_id)
    if not request.user.is_superuser and test.creator != request.user:
        messages.error(request, "Bạn không có quyền xem thử đề thi này.")
        return redirect('test_list')
        
    part_instructions = {
        pi.part_number: pi.instruction
        for pi in TestPartInstruction.objects.filter(test=test).select_related('instruction')
    }
    
    # Lấy tất cả TestQuestion sắp xếp theo order_index và id
    test_questions = TestQuestion.objects.filter(test=test).select_related('question').order_by('order_index', 'id')
    
    questions_by_part = {}
    for tq in test_questions:
        from lms.bbcode_parser import BBBienParser
        parser = BBBienParser(seed=test.id * 100000 + tq.question.id)
        
        tq.question.content = parser.parse(tq.question.content)
        
        choices = list(tq.question.choices.all().order_by('position', 'id'))
        for c in choices:
            c.content = parser.parse(c.content)
        
        # Xác định đáp án đúng để hiển thị nổi bật
        if tq.question.question_type == 4:
            correct_choices = choices
        else:
            correct_choices = [c for c in choices if c.is_correct]
        
        q_data = {
            'tq': tq,
            'choices': choices,
            'correct_choices': correct_choices,
        }
        
        part_num = tq.part_number
        if part_num not in questions_by_part:
            questions_by_part[part_num] = []
        questions_by_part[part_num].append(q_data)
        
    parts_to_render = []
    for part_num in sorted(questions_by_part.keys()):
        instruction = part_instructions.get(part_num)
        parts_to_render.append({
            'part': {
                'id': part_num,
                'title_roman': int_to_roman(part_num),
                'instruction': instruction.content if instruction else ""
            },
            'questions': questions_by_part[part_num]
        })
        
    return render(request, 'lms/preview_test.html', {
        'test': test,
        'parts': parts_to_render,
    })


@login_required
def search_questions_ajax(request):
    """Tìm kiếm nhanh ngân hàng câu hỏi bằng AJAX phục vụ chèn đề."""
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({'error': 'Bạn không có quyền thực hiện tác vụ này.'}, status=403)
        
    q = request.GET.get('q', '').strip()
    group_id = request.GET.get('group_id', '')
    q_type = request.GET.get('type', '')
    
    qs = Question.objects.all()
    if q:
        qs = qs.filter(Q(content__icontains=q))
    if group_id:
        qs = qs.filter(group_id=group_id)
    if q_type:
        qs = qs.filter(question_type=q_type)
        
    results = []
    for item in qs[:30]:
        results.append({
            'id': item.id,
            'type_display': item.get_question_type_display(),
            'group_name': item.group.name if item.group else 'Không phân nhóm',
            'content': item.content[:150] + ('...' if len(item.content) > 150 else ''),
            'is_public': item.is_public
        })
        
    return JsonResponse({'results': results})


@login_required
@csrf_exempt
def add_test_part_ajax(request, test_id):
    """AJAX: Thêm phần thi lớn mới (tạo TestPartInstruction)."""
    test = get_object_or_404(Test, id=test_id)
    if not request.user.is_superuser and test.creator != request.user:
        return JsonResponse({'error': 'Bạn không có quyền sửa đổi đề thi này.'}, status=403)
        
    if request.method == 'POST':
        import json
        try:
            data = json.loads(request.body)
            title_roman = data.get('title_roman', '').strip()
            instruction = data.get('instruction', '').strip()
            
            if not title_roman:
                return JsonResponse({'error': 'Số phần La Mã không được để trống (ví dụ: I, II).'}, status=400)
                
            part_number = roman_to_int(title_roman)
            
            if TestPartInstruction.objects.filter(test=test, part_number=part_number).exists():
                return JsonResponse({'error': f'Phần {title_roman} đã tồn tại trong đề thi này.'}, status=400)
                
            shared_inst = None
            if instruction:
                shared_inst = SharedInstruction.objects.create(
                    title=f"Lời dẫn Phần {title_roman} - {test.title[:30]}",
                    content=instruction
                )
                
            part_inst = TestPartInstruction.objects.create(
                test=test,
                part_number=part_number,
                instruction=shared_inst
            )
            
            return JsonResponse({
                'id': part_inst.id,
                'part_number': part_number,
                'title_roman': title_roman,
                'instruction': instruction,
                'shuffle_questions': False,
            })
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'error': 'Phương thức không hợp lệ.'}, status=405)


@login_required
@csrf_exempt
def delete_test_part_ajax(request, part_id):
    """AJAX: Xóa phần thi lớn (xóa TestPartInstruction và các câu hỏi thuộc phần)."""
    part_inst = get_object_or_404(TestPartInstruction, id=part_id)
    if not request.user.is_superuser and part_inst.test.creator != request.user:
        return JsonResponse({'error': 'Bạn không có quyền sửa đổi đề thi này.'}, status=403)
        
    if request.method == 'POST':
        try:
            # Xóa các TestQuestion thuộc phần này trong cùng đề thi
            TestQuestion.objects.filter(test=part_inst.test, part_number=part_inst.part_number).delete()
            # Xóa TestPartInstruction
            part_inst.delete()
            return JsonResponse({'success': True})
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'error': 'Phương thức không hợp lệ.'}, status=405)


@login_required
@csrf_exempt
def add_test_question_ajax(request, test_id):
    """AJAX: Liên kết câu hỏi từ Ngân hàng trực tiếp vào Đề thi."""
    test = get_object_or_404(Test, id=test_id)
    if not request.user.is_superuser and test.creator != request.user:
        return JsonResponse({'error': 'Bạn không có quyền sửa đổi đề thi này.'}, status=403)
        
    if request.method == 'POST':
        import json
        try:
            data = json.loads(request.body)
            question_id = data.get('question_id')
            points = float(data.get('points', 1.0))
            optional_type = data.get('optional_type', 'NONE')
            part_number = int(data.get('part_number', 1))
            force_add = data.get('force_add', False)
            
            question = get_object_or_404(Question, id=question_id)
            
            # Kiểm tra nhóm câu hỏi tương đương
            if question.equivalent_group and not force_add:
                already_has_eq = TestQuestion.objects.filter(
                    test=test,
                    question__equivalent_group=question.equivalent_group
                ).exists()
                if already_has_eq:
                    return JsonResponse({
                        'warning': True,
                        'message': f"Đề thi đã chứa câu hỏi thuộc nhóm tương đương '{question.equivalent_group.name}'. Bạn có chắc chắn muốn thêm câu hỏi này?"
                    })
            
            # Đảm bảo có TestPartInstruction tương ứng cho part_number này
            # (Giúp đồng bộ phần thi nếu chưa tồn tại lời dẫn)
            part_inst, _ = TestPartInstruction.objects.get_or_create(test=test, part_number=part_number)
            
            # Mặc định thứ tự của câu hỏi trong đề thi là 1, chứ không tăng dần
            order_index = 1
            
            tq = TestQuestion.objects.create(
                test=test,
                question=question,
                points=points,
                optional_type=optional_type,
                order_index=order_index,
                part_number=part_number
            )
            
            return JsonResponse({
                'id': tq.id,
                'question_id': question.id,
                'question_content': question.content[:100] + ('...' if len(question.content) > 100 else ''),
                'type_display': question.get_question_type_display(),
                'points': tq.points,
                'optional_type_display': tq.get_optional_type_display(),
                'order_index': tq.order_index,
                'part_number': part_number,
                'part_id': part_inst.id
            })
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'error': 'Phương thức không hợp lệ.'}, status=405)


@login_required
@csrf_exempt
def delete_test_question_ajax(request, tq_id):
    """AJAX: Gỡ liên kết câu hỏi khỏi phần thi."""
    tq = get_object_or_404(TestQuestion, id=tq_id)
    if not request.user.is_superuser and tq.test.creator != request.user:
        return JsonResponse({'error': 'Bạn không có quyền sửa đổi đề thi này.'}, status=403)
        
    if request.method == 'POST':
        try:
            tq.delete()
            return JsonResponse({'success': True})
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'error': 'Phương thức không hợp lệ.'}, status=405)


@login_required
@csrf_exempt
def update_test_question_ajax(request, tq_id):
    """AJAX: Cập nhật cấu hình của một câu hỏi trong đề thi (điểm, thứ tự, loại tự chọn, phần)."""
    tq = get_object_or_404(TestQuestion, id=tq_id)
    if not request.user.is_superuser and tq.test.creator != request.user:
        return JsonResponse({'error': 'Bạn không có quyền sửa đổi đề thi này.'}, status=403)
        
    if request.method == 'POST':
        import json
        try:
            data = json.loads(request.body)
            points = data.get('points')
            optional_type = data.get('optional_type')
            order_index = data.get('order_index')
            part_number = data.get('part_number')
            
            if points is not None:
                tq.points = float(points)
            if optional_type is not None:
                tq.optional_type = optional_type
            if order_index is not None:
                tq.order_index = int(order_index)
            if part_number is not None:
                part_num = int(part_number)
                if part_num < 1:
                    return JsonResponse({'error': 'Số phần phải lớn hơn hoặc bằng 1.'}, status=400)
                tq.part_number = part_num
                # Đảm bảo có TestPartInstruction tương ứng cho part_number này
                TestPartInstruction.objects.get_or_create(test=tq.test, part_number=part_num)
                
            tq.save()
            return JsonResponse({
                'success': True,
                'id': tq.id,
                'points': tq.points,
                'optional_type': tq.optional_type,
                'optional_type_display': tq.get_optional_type_display(),
                'order_index': tq.order_index,
                'part_number': tq.part_number
            })
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'error': 'Phương thức không hợp lệ.'}, status=405)


@csrf_exempt
@login_required
def add_questions_quick_ajax(request, test_id):
    """AJAX: Thêm nhanh danh sách câu hỏi bằng mã ID phân cách bởi khoảng trắng, phẩy, tab."""
    test = get_object_or_404(Test, id=test_id)
    if not request.user.is_superuser and test.creator != request.user:
        return JsonResponse({'error': 'Bạn không có quyền sửa đổi đề thi này.'}, status=403)

    if request.method == 'POST':
        import json
        import re
        try:
            data = json.loads(request.body)
            raw_ids = data.get('ids', '').strip()
            force_add = data.get('force_add', False)
            
            # Hỗ trợ cả dải liên tục như 2-6
            # Đưa các dải dạng "số - số" về dạng "số-số" bằng cách xóa khoảng trắng xung quanh dấu gạch ngang
            normalized_ids = re.sub(r'\s*-\s*', '-', raw_ids)
            
            # Tách chuỗi bằng dấu cách, phẩy, tab, xuống dòng...
            tokens = [x.strip() for x in re.split(r'[\s,\t\r\n]+', normalized_ids) if x.strip()]
            
            id_list = []
            for token in tokens:
                if token.isdigit():
                    id_list.append(int(token))
                else:
                    # Kiểm tra xem có khớp định dạng dải liên tục "start-end" không
                    range_match = re.match(r'^(\d+)-(\d+)$', token)
                    if range_match:
                        start = int(range_match.group(1))
                        end = int(range_match.group(2))
                        # Giới hạn dải tối đa 500 phần tử để tránh quá tải
                        if abs(end - start) <= 500:
                            if start <= end:
                                id_list.extend(range(start, end + 1))
                            else:
                                id_list.extend(range(start, end - 1, -1))
                        else:
                            return JsonResponse({'error': f'Phạm vi liên tục {token} quá lớn (tối đa 500 số).'}, status=400)
            
            if not id_list:
                return JsonResponse({'error': 'Không tìm thấy ID câu hỏi hợp lệ nào.'}, status=400)
                
            # Loại bỏ các phần tử trùng lặp
            id_list = list(dict.fromkeys(id_list))
            
            # Kiểm tra nhóm câu hỏi tương đương
            if not force_add:
                conflicts = []
                for q_id in id_list:
                    try:
                        question = Question.objects.get(id=q_id)
                        if question.equivalent_group:
                            in_db = TestQuestion.objects.filter(test=test, question__equivalent_group=question.equivalent_group).exists()
                            if in_db:
                                conflicts.append(f"Mã {q_id} (thuộc nhóm '{question.equivalent_group.name}')")
                    except Question.DoesNotExist:
                        pass
                if conflicts:
                    return JsonResponse({
                        'warning': True,
                        'message': "Các câu hỏi sau có nhóm tương đương đã tồn tại trong đề thi:\n" + "\n".join(conflicts) + "\n\nBạn có chắc chắn muốn tiếp tục thêm các câu này không?"
                    })
            
            added_questions = []
            errors = []
            
            for q_id in id_list:
                try:
                    question = Question.objects.get(id=q_id)
                    # Kiểm tra trùng lặp câu hỏi trong đề
                    if TestQuestion.objects.filter(test=test, question=question).exists():
                        errors.append(f"Mã {q_id} đã tồn tại trong đề thi.")
                        continue
                        
                    tq = TestQuestion.objects.create(
                        test=test,
                        question=question,
                        points=1.0,
                        optional_type='NONE',
                        order_index=1,
                        part_number=1
                    )
                    added_questions.append({
                        'id': tq.id,
                        'question_id': question.id,
                        'question_content': question.content[:70],
                        'type_display': question.get_question_type_display(),
                        'points': tq.points,
                        'optional_type': tq.optional_type,
                        'part_number': tq.part_number,
                        'order_index': tq.order_index
                    })
                except Question.DoesNotExist:
                    errors.append(f"Mã {q_id} không tồn tại trên hệ thống.")
            
            # Đảm bảo có TestPartInstruction mặc định cho phần 1
            TestPartInstruction.objects.get_or_create(test=test, part_number=1)
            
            return JsonResponse({
                'success': True,
                'added': added_questions,
                'errors': errors
            })
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'error': 'Phương thức không hợp lệ.'}, status=405)


@csrf_exempt
@login_required
def update_part_instruction_ajax(request, test_id):
    """AJAX: Thêm/Sửa/Xóa lời dẫn cho từng phần thi (số nguyên)."""
    test = get_object_or_404(Test, id=test_id)
    if not request.user.is_superuser and test.creator != request.user:
        return JsonResponse({'error': 'Bạn không có quyền sửa đổi đề thi này.'}, status=403)

    if request.method == 'POST':
        import json
        try:
            data = json.loads(request.body)
            part_number = int(data.get('part_number', 1))
            instruction_id = data.get('instruction_id') # ID của SharedInstruction được chọn
            title = data.get('title', '').strip()
            content = data.get('content', '').strip()

            if part_number < 1:
                return JsonResponse({'error': 'Số phần phải lớn hơn hoặc bằng 1.'}, status=400)

            part_inst, created = TestPartInstruction.objects.get_or_create(test=test, part_number=part_number)
            old_shared = part_inst.instruction

            if instruction_id:
                # 1. Chọn lời dẫn có sẵn
                shared_inst = SharedInstruction.objects.filter(id=instruction_id).first()
                if not shared_inst:
                    return JsonResponse({'error': 'Lời dẫn đã chọn không tồn tại.'}, status=404)
                
                if old_shared != shared_inst:
                    part_inst.instruction = shared_inst
                    part_inst.save()
                    
                    # Dọn dẹp old_shared nếu không còn ai tham chiếu tới nó
                    if old_shared and not TestPartInstruction.objects.filter(instruction=old_shared).exclude(id=part_inst.id).exists():
                        old_shared.delete()
                        
                return JsonResponse({
                    'success': True,
                    'part_number': part_number,
                    'content': shared_inst.content,
                    'instruction_id': shared_inst.id,
                    'instruction_title': shared_inst.title,
                    'message': 'Đã áp dụng lời dẫn có sẵn thành công.'
                })
            
            elif content:
                # 2. Thêm mới lời dẫn
                # Tìm xem đã có SharedInstruction nào có nội dung giống hệt chưa (Chống trùng lặp)
                shared_inst = SharedInstruction.objects.filter(content=content).first()
                
                if shared_inst:
                    # Nếu có sẵn, ta dùng luôn tham chiếu đó
                    if old_shared != shared_inst:
                        part_inst.instruction = shared_inst
                        part_inst.save()
                        
                        # Dọn dẹp old_shared
                        if old_shared and not TestPartInstruction.objects.filter(instruction=old_shared).exclude(id=part_inst.id).exists():
                            old_shared.delete()
                else:
                    # Tạo mới SharedInstruction hoàn toàn
                    if not title:
                        title = f"Lời dẫn Phần {part_number} - {test.title[:30]}"
                    
                    new_shared = SharedInstruction.objects.create(
                        title=title,
                        content=content
                    )
                    part_inst.instruction = new_shared
                    part_inst.save()
                    shared_inst = new_shared

                return JsonResponse({
                    'success': True,
                    'part_number': part_number,
                    'content': shared_inst.content,
                    'instruction_id': shared_inst.id,
                    'instruction_title': shared_inst.title,
                    'message': 'Đã tạo và áp dụng lời dẫn mới thành công.'
                })
            else:
                # 3. Gỡ bỏ lời dẫn (Xóa)
                if old_shared:
                    part_inst.instruction = None
                    part_inst.save()
                    
                    # Dọn dẹp old_shared nếu không còn ai sử dụng
                    if not TestPartInstruction.objects.filter(instruction=old_shared).exclude(id=part_inst.id).exists():
                        old_shared.delete()
                
                return JsonResponse({
                    'success': True,
                    'part_number': part_number,
                    'content': '',
                    'instruction_id': None,
                    'instruction_title': '',
                    'message': 'Đã xóa lời dẫn thành công.'
                })
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'error': 'Phương thức không hợp lệ.'}, status=405)


@csrf_exempt
@login_required
def update_shared_instruction_ajax(request, instruction_id):
    """AJAX: Sửa đổi trực tiếp SharedInstruction."""
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({'error': 'Bạn không có quyền thực hiện tác vụ này.'}, status=403)
        
    shared_inst = get_object_or_404(SharedInstruction, id=instruction_id)
    if request.method == 'POST':
        import json
        try:
            data = json.loads(request.body)
            title = data.get('title', '').strip()
            content = data.get('content', '').strip()
            
            if not content:
                return JsonResponse({'error': 'Nội dung lời dẫn không được để trống.'}, status=400)
                
            shared_inst.title = title or shared_inst.title
            shared_inst.content = content
            shared_inst.save()
            
            return JsonResponse({
                'success': True,
                'id': shared_inst.id,
                'title': shared_inst.title,
                'content': shared_inst.content,
                'message': 'Cập nhật lời dẫn dùng chung thành công. Lưu ý: Thay đổi sẽ áp dụng cho tất cả các phần/đề thi đang sử dụng lời dẫn này.'
            })
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'error': 'Phương thức không hợp lệ.'}, status=405)


# --- HỆ THỐNG IMPORT CÂU HỎI SỐ LƯỢNG LỚN (AIKEN & CSV PARSER) ---
import re
import csv
import io
import json

def parse_question_block(block_text):
    """
    Phân tích một khối văn bản câu hỏi dạng Aiken / Aiken Mở rộng.
    """
    lines = [line.strip() for line in block_text.split('\n') if line.strip()]
    if not lines:
        return None
        
    # Tìm dòng chứa đáp án
    ans_line = None
    ans_idx = -1
    for idx, line in enumerate(lines):
        if re.match(r'^(Đáp án|Đáp án đúng|ANSWER|Answer)\s*[:\-]?\s*(.*)$', line, re.IGNORECASE):
            ans_line = line
            ans_idx = idx
            break
            
    if ans_line is None:
        return {
            'content': block_text,
            'error': 'Thiếu dòng khai báo đáp án ("Đáp án:" hoặc "ANSWER:") ở cuối câu hỏi.',
            'valid': False
        }
        
    ans_match = re.match(r'^(Đáp án|Đáp án đúng|ANSWER|Answer)\s*[:\-]?\s*(.*)$', ans_line, re.IGNORECASE)
    ans_val = ans_match.group(2).strip()
    
    q_lines = []
    choices = []
    
    for idx, line in enumerate(lines):
        if idx == ans_idx:
            continue
        # Nhận diện dòng lựa chọn (A., B., C., D. hoặc A), B), C), D))
        choice_match = re.match(r'^([A-Fa-f])[\.\)]\s*(.*)$', line)
        if choice_match:
            choices.append({
                'label': choice_match.group(1).upper(),
                'text': choice_match.group(2).strip()
            })
        else:
            if idx < ans_idx:
                q_lines.append(line)
                
    q_content = "\n".join(q_lines).strip()
    
    # Loại bỏ tiền tố tiêu đề câu hỏi nếu có (e.g. "Câu 1:", "Câu hỏi 1:")
    q_content = re.sub(r'^(Câu|Câu hỏi)\s*\d+\s*[:\-\.]?\s*', '', q_content, flags=re.IGNORECASE).strip()
    
    if not q_content:
        return {
            'content': block_text,
            'error': 'Nội dung câu hỏi không được để trống.',
            'valid': False
        }
        
    # PHÂN LOẠI HÌNH THỨC CÂU HỎI
    
    # 1. Trả lời ngắn (Loại 4)
    if not choices:
        answers = [a.strip() for a in ans_val.split('|') if a.strip()]
        if not answers:
            return {
                'content': q_content,
                'error': 'Câu hỏi trả lời ngắn cần ít nhất một đáp án điền đúng được ngăn cách bởi dấu |.',
                'valid': False
            }
        return {
            'content': q_content,
            'type': 4,
            'choices': [{'content': ans, 'is_correct': False} for ans in answers], # Type 4 lưu is_case_sensitive ở is_correct
            'valid': True
        }
        
    # 2. Đúng / Sai 4 ý độc lập (Loại 3)
    is_tf = False
    tf_answers = {}
    if any(keyword in ans_val.lower() for keyword in ['đúng', 'sai', 'true', 'false', 'correct', 'incorrect']):
        is_tf = True
        for choice in choices:
            label = choice['label']
            tf_match = re.search(rf'{label}\s*[-:]\s*(Đúng|Sai|True|False|T|F)', ans_val, re.IGNORECASE)
            if tf_match:
                val = tf_match.group(1).lower()
                tf_answers[label] = val in ['đúng', 'true', 't']
            else:
                free_match = re.search(rf'{label}\s+(Đúng|Sai|True|False)', ans_val, re.IGNORECASE)
                if free_match:
                    val = free_match.group(1).lower()
                    tf_answers[label] = val in ['đúng', 'true']
                else:
                    tf_answers[label] = False
                    
    if is_tf:
        if len(choices) != 4:
            return {
                'content': q_content,
                'error': f'Câu hỏi Đúng/Sai 4 ý bắt buộc phải khai báo đủ 4 phương án lựa chọn A, B, C, D (Hiện tại có {len(choices)} phương án).',
                'valid': False
            }
        return {
            'content': q_content,
            'type': 3,
            'choices': [{'content': c['text'], 'is_correct': tf_answers.get(c['label'], False)} for c in choices],
            'valid': True
        }
        
    # 3. Trắc nghiệm 1 hoặc nhiều đáp án (Loại 1, Loại 2)
    correct_labels = [l.strip().upper() for l in re.split(r'[\s,\s;]+', ans_val) if l.strip()]
    correct_labels = [l for l in correct_labels if l in [c['label'] for c in choices]]
    
    if not correct_labels:
        return {
            'content': q_content,
            'error': f'Không xác định được đáp án đúng từ chuỗi "{ans_val}". Đáp án phải tương ứng với các nhãn lựa chọn.',
            'valid': False
        }
        
    q_type = 2 if len(correct_labels) > 1 else 1
    return {
        'content': q_content,
        'type': q_type,
        'choices': [{'content': c['text'], 'is_correct': c['label'] in correct_labels} for c in choices],
        'valid': True
    }


@login_required
def question_import(request):
    if not (request.user.is_staff or request.user.is_superuser):
        messages.error(request, "Bạn không có quyền truy cập chức năng Import Câu hỏi.")
        return redirect('question_list')
        
    groups = QuestionGroup.objects.all()
    tests = Test.objects.all().order_by('-id')
    default_test_id = request.GET.get('test_id', '')
    return render(request, 'lms/question_import.html', {
        'groups': groups,
        'tests': tests,
        'default_test_id': default_test_id
    })


@login_required
@csrf_exempt
def parse_import_ajax(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({'error': 'Bạn không có quyền thực hiện chức năng này.'}, status=403)
        
    if request.method != 'POST':
        return JsonResponse({'error': 'Phương thức không hợp lệ.'}, status=405)
        
    text_content = request.POST.get('text_content', '').strip()
    csv_file = request.FILES.get('file')
    
    parsed_questions = []
    
    if csv_file:
        try:
            file_data = csv_file.read()
            for encoding in ['utf-8-sig', 'utf-8', 'windows-1258', 'utf-16']:
                try:
                    decoded_file = file_data.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                decoded_file = file_data.decode('utf-8', errors='replace')
                
            io_string = io.StringIO(decoded_file)
            reader = csv.reader(io_string, delimiter=',')
            
            rows = list(reader)
            if len(rows) < 2:
                return JsonResponse({'error': 'Tệp CSV không chứa dữ liệu hoặc thiếu tiêu đề.'}, status=400)
                
            for idx, row in enumerate(rows[1:]):
                if len(row) < 2:
                    continue
                q_content = row[0].strip()
                q_type_str = row[1].strip()
                if not q_content or not q_type_str:
                    continue
                    
                try:
                    q_type = int(q_type_str)
                except ValueError:
                    parsed_questions.append({
                        'content': q_content,
                        'error': f'Dòng {idx+2}: Loại câu hỏi "{q_type_str}" không hợp lệ (phải là số từ 1 đến 4).',
                        'valid': False
                    })
                    continue
                    
                choices = []
                for col_idx, label in [(2, 'A'), (3, 'B'), (4, 'C'), (5, 'D')]:
                    if col_idx < len(row):
                        val = row[col_idx].strip()
                        if val:
                            choices.append({'label': label, 'text': val})
                            
                ans_val = row[6].strip() if 6 < len(row) else ''
                
                if q_type == 4:
                    answers = [a.strip() for a in ans_val.split('|') if a.strip()]
                    if not answers:
                        parsed_questions.append({
                            'content': q_content,
                            'error': f'Dòng {idx+2}: Câu hỏi trả lời ngắn cần ít nhất một đáp án đúng trong cột Đáp án.',
                            'valid': False
                        })
                    else:
                        parsed_questions.append({
                            'content': q_content,
                            'type': 4,
                            'choices': [{'content': ans, 'is_correct': False} for ans in answers],
                            'valid': True
                        })
                elif q_type == 3:
                    if len(choices) != 4:
                        parsed_questions.append({
                            'content': q_content,
                            'error': f'Dòng {idx+2}: Câu hỏi Đúng/Sai bắt buộc phải điền đủ 4 phương án A, B, C, D.',
                            'valid': False
                        })
                        continue
                        
                    tf_answers = {}
                    for c in choices:
                        label = c['label']
                        tf_match = re.search(rf'{label}\s*[-:]\s*(Đúng|Sai|True|False|T|F)', ans_val, re.IGNORECASE)
                        if tf_match:
                            val = tf_match.group(1).lower()
                            tf_answers[label] = val in ['đúng', 'true', 't']
                        else:
                            parts = [p.strip() for p in ans_val.split(',') if p.strip()]
                            if len(parts) == 4:
                                pos_label_idx = ord(label) - 65
                                val = parts[pos_label_idx].lower()
                                tf_answers[label] = val in ['đúng', 'true', 't', '1']
                            else:
                                tf_answers[label] = False
                                
                    parsed_questions.append({
                        'content': q_content,
                        'type': 3,
                        'choices': [{'content': c['text'], 'is_correct': tf_answers.get(c['label'], False)} for c in choices],
                        'valid': True
                    })
                elif q_type in [1, 2]:
                    if not choices:
                        parsed_questions.append({
                            'content': q_content,
                            'error': f'Dòng {idx+2}: Câu hỏi trắc nghiệm cần có ít nhất một phương án lựa chọn.',
                            'valid': False
                        })
                        continue
                        
                    correct_labels = [l.strip().upper() for l in re.split(r'[\s,\s;]+', ans_val) if l.strip()]
                    correct_labels = [l for l in correct_labels if l in [c['label'] for c in choices]]
                    
                    if not correct_labels:
                        parsed_questions.append({
                            'content': q_content,
                            'error': f'Dòng {idx+2}: Không tìm thấy đáp án đúng hợp lệ trong cột Đáp án "{ans_val}".',
                            'valid': False
                        })
                        continue
                        
                    parsed_questions.append({
                        'content': q_content,
                        'type': q_type,
                        'choices': [{'content': c['text'], 'is_correct': c['label'] in correct_labels} for c in choices],
                        'valid': True
                    })
                else:
                    parsed_questions.append({
                        'content': q_content,
                        'error': f'Dòng {idx+2}: Loại câu hỏi {q_type} không được hỗ trợ.',
                        'valid': False
                    })
        except Exception as e:
            return JsonResponse({'error': f'Lỗi đọc tệp tin CSV: {str(e)}'}, status=400)
            
    elif text_content:
        blocks = re.split(r'\n\s*\n', text_content)
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            parsed = parse_question_block(block)
            if parsed:
                parsed_questions.append(parsed)
    else:
        return JsonResponse({'error': 'Vui lòng nhập văn bản thô hoặc tải lên tệp CSV.'}, status=400)
        
    return JsonResponse({
        'success': True,
        'questions': parsed_questions,
        'total_parsed': len(parsed_questions),
        'total_valid': sum(1 for q in parsed_questions if q.get('valid', False))
    })


@login_required
@csrf_exempt
def save_import_ajax(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({'error': 'Bạn không có quyền thực hiện chức năng này.'}, status=403)
        
    if request.method != 'POST':
        return JsonResponse({'error': 'Phương thức không hợp lệ.'}, status=405)
        
    try:
        data = json.loads(request.body)
        questions_data = data.get('questions', [])
        group_id = data.get('group_id')
        test_id = data.get('test_id')
        
        if not questions_data:
            return JsonResponse({'error': 'Danh sách câu hỏi trống.'}, status=400)
            
        group = None
        if group_id:
            group = QuestionGroup.objects.filter(id=group_id).first()
            
        test = None
        if test_id:
            test = Test.objects.filter(id=test_id).first()
            if not test:
                return JsonResponse({'error': 'Đề thi đã chọn không tồn tại.'}, status=400)
            
        saved_count = 0
        with transaction.atomic():
            next_order = 1
            if test:
                # Ensure part 1 has instruction
                TestPartInstruction.objects.get_or_create(test=test, part_number=1)
                # Calculate starting order_index
                from django.db.models import Max
                current_max = TestQuestion.objects.filter(test=test, part_number=1).aggregate(Max('order_index'))['order_index__max'] or 0
                next_order = current_max + 1

            for q_item in questions_data:
                if not q_item.get('valid', False):
                    continue
                    
                q_obj = Question.objects.create(
                    group=group,
                    content=q_item['content'],
                    question_type=q_item['type'],
                    is_public=True,
                    creator=request.user
                )
                
                for idx, c_item in enumerate(q_item['choices']):
                    Choice.objects.create(
                        question=q_obj,
                        content=c_item['content'],
                        is_correct=c_item['is_correct'],
                        position=1
                    )
                saved_count += 1
                
                if test:
                    TestQuestion.objects.create(
                        test=test,
                        question=q_obj,
                        points=1.0,
                        optional_type='NONE',
                        order_index=next_order,
                        part_number=1
                    )
                    next_order += 1
                
        return JsonResponse({
            'success': True,
            'count': saved_count,
            'message': f'Đã import thành công {saved_count} câu hỏi vào hệ thống!'
        })
    except Exception as e:
        return JsonResponse({'error': f'Lỗi hệ thống khi lưu: {str(e)}'}, status=500)


@login_required
def download_csv_template(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return HttpResponse("Bạn không có quyền thực hiện hành động này.", status=403)
        
    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    response['Content-Disposition'] = 'attachment; filename="hoctin_import_mau.csv"'
    
    # Ghi BOM cho Excel hiển thị đúng Tiếng Việt
    response.write(b'\xef\xbb\xbf')
    
    writer = csv.writer(response, delimiter=',')
    writer.writerow([
        'Nội dung câu hỏi',
        'Hình thức (1: 1 lựa chọn, 2: Nhiều lựa chọn, 3: Đúng/Sai, 4: Trả lời ngắn)',
        'Lựa chọn A (Bỏ trống nếu loại 4)',
        'Lựa chọn B (Bỏ trống nếu loại 4)',
        'Lựa chọn C (Bỏ trống nếu loại 4)',
        'Lựa chọn D (Bỏ trống nếu loại 4)',
        'Đáp án đúng (Loại 1: A; Loại 2: A,B; Loại 3: A: Đúng, B: Sai, C: Đúng, D: Sai; Loại 4: Đáp án 1 | Đáp án 2)'
    ])
    
    # Viết dữ liệu mẫu để giáo viên hiểu tức thì
    writer.writerow([
        'Thủ đô của Việt Nam là gì?',
        '1',
        'Hà Nội',
        'Thành phố Hồ Chí Minh',
        'Đà Nẵng',
        'Hải Phòng',
        'A'
    ])
    writer.writerow([
        'Các tỉnh nào sau đây thuộc vùng Tây Nguyên?',
        '2',
        'Lâm Đồng',
        'Đắk Lắk',
        'Cần Thơ',
        'Gia Lai',
        'A, B, D'
    ])
    writer.writerow([
        'Cho hàm số y = x^2 - 2x. Các mệnh đề sau đúng hay sai?',
        '3',
        'Hàm số nghịch biến trên khoảng (-vô cùng; 1)',
        'Đồ thị hàm số có đỉnh I(1; -1)',
        'Hàm số đồng biến trên khoảng (1; +vô cùng)',
        'Giá trị nhỏ nhất của hàm số là 1',
        'A: Đúng, B: Đúng, C: Đúng, D: Sai'
    ])
    writer.writerow([
        'Kết quả của phép tính 2^3 * 5 là bao nhiêu?',
        '4',
        '',
        '',
        '',
        '',
        '40 | Bốn mươi'
    ])
    
    return response


def test_bundle_list(request):
    """Hiển thị danh sách tất cả các gói đề luyện tập."""
    bundles = TestBundle.objects.all()
    # Xác định trạng thái sở hữu của từng gói đề đối với học sinh hiện tại
    for b in bundles:
        all_tests = b.tests.all()
        if not all_tests.exists():
            b.is_owned = False
            continue
        if request.user.is_authenticated:
            owned_count = TestOwnership.objects.filter(user=request.user, test__in=all_tests).count()
            b.is_owned = (owned_count == all_tests.count())
        else:
            b.is_owned = False
    return render(request, 'lms/test_bundle_list.html', {'bundles': bundles})


def test_bundle_detail(request, bundle_id):
    """Hiển thị chi tiết của gói đề thi, liệt kê danh sách các đề thi bên trong."""
    bundle = get_object_or_404(TestBundle, id=bundle_id)
    tests = bundle.tests.all()
    
    # Xác định trạng thái sở hữu riêng của từng đề thi bên trong gói đề
    tests_with_status = []
    owned_count = 0
    for t in tests:
        is_owned = False
        if request.user.is_authenticated:
            is_owned = TestOwnership.objects.filter(user=request.user, test=t).exists()
            if is_owned:
                owned_count += 1
        tests_with_status.append({
            'test': t,
            'is_owned': is_owned
        })
        
    is_fully_owned = (tests.exists() and owned_count == tests.count())
    
    has_pending_transaction = False
    if request.user.is_authenticated and not is_fully_owned:
        has_pending_transaction = WalletTransaction.objects.filter(
            user=request.user,
            test_bundle=bundle,
            status='PENDING'
        ).exists()
    
    return render(request, 'lms/test_bundle_detail.html', {
        'bundle': bundle,
        'tests_with_status': tests_with_status,
        'is_fully_owned': is_fully_owned,
        'has_pending_transaction': has_pending_transaction,
    })


@login_required
def buy_test_bundle(request, bundle_id):
    """Xử lý giao dịch ví để mua trọn bộ đề thi trong gói luyện tập."""
    bundle = get_object_or_404(TestBundle, id=bundle_id)
    profile = request.user.profile
    
    tests = bundle.tests.all()
    if not tests.exists():
        messages.warning(request, "Gói đề thi này hiện chưa có đề thi nào.")
        return redirect('test_bundle_detail', bundle_id=bundle.id)
        
    # Kiểm tra xem người dùng đã sở hữu toàn bộ các đề thi trong gói chưa
    owned_count = TestOwnership.objects.filter(user=request.user, test__in=tests).count()
    if owned_count == tests.count():
        messages.info(request, "Bạn đã sở hữu toàn bộ đề thi trong gói này.")
        return redirect('test_bundle_detail', bundle_id=bundle.id)
        
    if profile.wallet_balance < bundle.price:
        messages.error(request, "Số dư tài khoản ví không đủ để thanh toán gói đề này. Vui lòng nạp thêm tiền.")
        return redirect('wallet_deposit')
        
    with transaction.atomic():
        # Khấu trừ tiền ví
        profile.wallet_balance -= bundle.price
        profile.save()
        
        # Cấp quyền sở hữu TestOwnership cho tất cả đề thi có trong gói
        for test in tests:
            TestOwnership.objects.get_or_create(
                user=request.user,
                test=test,
                defaults={'purchased_at': timezone.now(), 'agreed_rules': True}
            )
            
        # Lưu vết giao dịch
        WalletTransaction.objects.create(
            user=request.user,
            amount=bundle.price,
            transaction_type='PAYMENT',
            status='APPROVED'
        )
        
    messages.success(request, f"Chúc mừng! Bạn đã mua thành công gói đề '{bundle.title}'.")
    return redirect('test_bundle_detail', bundle_id=bundle.id)


def course_bundle_list(request):
    """Hiển thị danh sách các gói khóa học."""
    bundles = CourseBundle.objects.all()
    for b in bundles:
        all_courses = b.courses.all()
        # Tính tổng giá gốc của các khóa học lẻ trong gói để hiển thị tiết kiệm
        b.original_total = sum(c.price for c in all_courses)
        b.saving_amount = max(0, b.original_total - b.price)
        
        if not all_courses.exists():
            b.is_owned = False
            continue
        if request.user.is_authenticated:
            owned_count = sum(1 for c in all_courses if CourseOwnership.has_active_ownership(request.user, c))
            b.is_owned = (owned_count == all_courses.count())
        else:
            b.is_owned = False
    return render(request, 'lms/course_bundle_list.html', {'bundles': bundles})


def course_bundle_detail(request, bundle_id):
    """Chi tiết gói khóa học, danh sách các khóa học bên trong."""
    bundle = get_object_or_404(CourseBundle, id=bundle_id)
    courses = bundle.courses.all()
    
    courses_with_status = []
    owned_count = 0
    original_total = 0
    for c in courses:
        original_total += c.price
        is_owned = False
        if request.user.is_authenticated:
            is_owned = CourseOwnership.has_active_ownership(request.user, c)
            if is_owned:
                owned_count += 1
        courses_with_status.append({
            'course': c,
            'is_owned': is_owned
        })
        
    is_fully_owned = (courses.exists() and owned_count == courses.count())
    saving_amount = max(0, original_total - bundle.price)
    
    has_pending_transaction = False
    if request.user.is_authenticated and not is_fully_owned:
        has_pending_transaction = WalletTransaction.objects.filter(
            user=request.user,
            course_bundle=bundle,
            status='PENDING'
        ).exists()
    
    return render(request, 'lms/course_bundle_detail.html', {
        'bundle': bundle,
        'courses_with_status': courses_with_status,
        'is_fully_owned': is_fully_owned,
        'original_total': original_total,
        'saving_amount': saving_amount,
        'has_pending_transaction': has_pending_transaction,
    })


@login_required
def buy_course_bundle(request, bundle_id):
    """Mua trọn bộ các khóa học trong gói khóa học."""
    bundle = get_object_or_404(CourseBundle, id=bundle_id)
    profile = request.user.profile
    
    courses = bundle.courses.all()
    if not courses.exists():
        messages.warning(request, "Gói khóa học này hiện chưa có khóa học nào.")
        return redirect('course_bundle_detail', bundle_id=bundle.id)
        
    # Kiểm tra xem người dùng đã sở hữu toàn bộ các khóa học trong gói chưa
    owned_count = sum(1 for c in courses if CourseOwnership.has_active_ownership(request.user, c))
    if owned_count == courses.count():
        messages.info(request, "Bạn đã sở hữu toàn bộ khóa học trong gói này rồi.")
        return redirect('course_bundle_detail', bundle_id=bundle.id)
        
    if profile.wallet_balance < bundle.price:
        messages.error(request, "Số dư tài khoản ví không đủ để thanh toán gói khóa học này. Vui lòng nạp thêm tiền.")
        return redirect('wallet_deposit')
        
    with transaction.atomic():
        # Khấu trừ tiền ví
        profile.wallet_balance -= bundle.price
        profile.save()
        
        # Cấp quyền sở hữu CourseOwnership cho tất cả khóa học có trong gói
        for course in courses:
            expires_at = None
            if course.duration_days and course.duration_days > 0:
                expires_at = timezone.now() + timezone.timedelta(days=course.duration_days)
            ownership, created = CourseOwnership.objects.get_or_create(
                user=request.user,
                course=course,
                defaults={'expires_at': expires_at}
            )
            if not created:
                ownership.expires_at = expires_at
                ownership.purchased_at = timezone.now()
                ownership.save()
            
        # Lưu vết giao dịch
        WalletTransaction.objects.create(
            user=request.user,
            amount=bundle.price,
            transaction_type='PAYMENT',
            status='APPROVED'
        )
        
    messages.success(request, f"Chúc mừng! Bạn đã mua thành công gói khóa học '{bundle.title}'.")
    return redirect('course_bundle_detail', bundle_id=bundle.id)


@login_required
def course_checkout(request, course_id):
    course = get_object_or_404(Course, id=course_id)
    
    # Kiểm tra xem học sinh đã sở hữu khóa học chưa
    if CourseOwnership.has_active_ownership(request.user, course):
        messages.info(request, "Bạn đã sở hữu khóa học này.")
        return redirect('course_detail', course_id=course.id)
        
    # Kiểm tra xem có giao dịch đang chờ duyệt không
    pending_tx = WalletTransaction.objects.filter(
        user=request.user,
        course=course,
        status='PENDING'
    ).first()
    if pending_tx:
        messages.warning(request, "Bạn đang có một yêu cầu thanh toán chuyển khoản chờ duyệt cho khóa học này.")
        return redirect('course_detail', course_id=course.id)
        
    profile = request.user.profile
    bank_accounts = BankAccount.objects.filter(is_active=True)
    
    if request.method == 'POST':
        payment_method = request.POST.get('payment_method')
        
        if payment_method == 'offline':
            proof_image = request.FILES.get('proof_image')
            bank_account_id = request.POST.get('bank_account_id')
            bank_account = None
            if bank_account_id:
                try:
                    bank_account = BankAccount.objects.get(id=bank_account_id, is_active=True)
                except BankAccount.DoesNotExist:
                    pass
            
            if not proof_image:
                messages.error(request, "Vui lòng tải lên ảnh chụp giao dịch / biên lai chuyển khoản.")
                return render(request, 'lms/checkout.html', {
                    'item': course,
                    'item_type': 'course',
                    'profile': profile,
                    'price': course.price,
                    'bank_accounts': bank_accounts,
                })
                
            WalletTransaction.objects.create(
                user=request.user,
                amount=course.price,
                transaction_type='PAYMENT',
                status='PENDING',
                proof_image=proof_image,
                course=course,
                bank_account=bank_account
            )
            messages.success(
                request, 
                "Gửi yêu cầu thanh toán thành công! Quyền sở hữu khóa học sẽ được mở sau khi Admin duyệt biên lai của bạn."
            )
            return redirect('course_detail', course_id=course.id)
            
    return render(request, 'lms/checkout.html', {
        'item': course,
        'item_type': 'course',
        'profile': profile,
        'price': course.price,
        'bank_accounts': bank_accounts,
    })


@login_required
def course_bundle_checkout(request, bundle_id):
    bundle = get_object_or_404(CourseBundle, id=bundle_id)
    courses = bundle.courses.all()
    
    # Kiểm tra xem đã sở hữu trọn combo chưa
    owned_count = sum(1 for c in courses if CourseOwnership.has_active_ownership(request.user, c))
    if courses.exists() and owned_count == courses.count():
        messages.info(request, "Bạn đã sở hữu toàn bộ các khóa học trong gói combo này.")
        return redirect('course_bundle_detail', bundle_id=bundle.id)
        
    pending_tx = WalletTransaction.objects.filter(
        user=request.user,
        course_bundle=bundle,
        status='PENDING'
    ).first()
    if pending_tx:
        messages.warning(request, "Bạn đang có một yêu cầu thanh toán chuyển khoản chờ duyệt cho gói combo này.")
        return redirect('course_bundle_detail', bundle_id=bundle.id)
        
    profile = request.user.profile
    bank_accounts = BankAccount.objects.filter(is_active=True)
    
    if request.method == 'POST':
        payment_method = request.POST.get('payment_method')
        
        if payment_method == 'offline':
            proof_image = request.FILES.get('proof_image')
            bank_account_id = request.POST.get('bank_account_id')
            bank_account = None
            if bank_account_id:
                try:
                    bank_account = BankAccount.objects.get(id=bank_account_id, is_active=True)
                except BankAccount.DoesNotExist:
                    pass
                
            if not proof_image:
                messages.error(request, "Vui lòng tải lên ảnh chụp giao dịch / biên lai chuyển khoản.")
                return render(request, 'lms/checkout.html', {
                    'item': bundle,
                    'item_type': 'course_bundle',
                    'profile': profile,
                    'price': bundle.price,
                    'bank_accounts': bank_accounts,
                })
                
            WalletTransaction.objects.create(
                user=request.user,
                amount=bundle.price,
                transaction_type='PAYMENT',
                status='PENDING',
                proof_image=proof_image,
                course_bundle=bundle,
                bank_account=bank_account
            )
            messages.success(
                request, 
                "Gửi yêu cầu thanh toán thành công! Quyền sở hữu gói combo sẽ được mở sau khi Admin duyệt biên lai của bạn."
            )
            return redirect('course_bundle_detail', bundle_id=bundle.id)
            
    return render(request, 'lms/checkout.html', {
        'item': bundle,
        'item_type': 'course_bundle',
        'profile': profile,
        'price': bundle.price,
        'bank_accounts': bank_accounts,
    })


@login_required
def test_checkout(request, test_id):
    test = get_object_or_404(Test, id=test_id)
    
    # Kiểm tra xem học sinh đã có TestOwnership chưa
    existing_reg = TestOwnership.objects.filter(user=request.user, test=test).first()
    if existing_reg and existing_reg.agreed_rules:
        messages.info(request, "Bạn đã đăng ký dự thi bài thi này thành công trước đó.")
        return redirect('test_detail', test_id=test.id)
        
    pending_tx = WalletTransaction.objects.filter(
        user=request.user,
        test=test,
        status='PENDING'
    ).first()
    if pending_tx:
        messages.warning(request, "Bạn đang có một yêu cầu thanh toán chuyển khoản chờ duyệt cho đề thi này.")
        return redirect('test_detail', test_id=test.id)
        
    profile = request.user.profile
    bank_accounts = BankAccount.objects.filter(is_active=True)
    
    if request.method == 'POST':
        payment_method = request.POST.get('payment_method')
        agree = request.POST.get('agree_rules') == 'on'
        
        if not agree:
            messages.error(request, "Bạn phải tích chọn đồng ý với Quy chế phòng thi trước khi thanh toán.")
            return render(request, 'lms/checkout.html', {
                'item': test,
                'item_type': 'test',
                'profile': profile,
                'price': test.price,
                'bank_accounts': bank_accounts,
            })
            
        if payment_method == 'offline':
            proof_image = request.FILES.get('proof_image')
            bank_account_id = request.POST.get('bank_account_id')
            bank_account = None
            if bank_account_id:
                try:
                    bank_account = BankAccount.objects.get(id=bank_account_id, is_active=True)
                except BankAccount.DoesNotExist:
                    pass

            if not proof_image:
                messages.error(request, "Vui lòng tải lên ảnh chụp giao dịch / biên lai chuyển khoản.")
                return render(request, 'lms/checkout.html', {
                    'item': test,
                    'item_type': 'test',
                    'profile': profile,
                    'price': test.price,
                    'bank_accounts': bank_accounts,
                })
                
            WalletTransaction.objects.create(
                user=request.user,
                amount=test.price,
                transaction_type='PAYMENT',
                status='PENDING',
                proof_image=proof_image,
                test=test,
                bank_account=bank_account
            )
            messages.success(
                request, 
                "Gửi yêu cầu thanh toán thành công! Quyền dự thi sẽ được mở sau khi Admin duyệt biên lai của bạn."
            )
            return redirect('test_detail', test_id=test.id)
            
    return render(request, 'lms/checkout.html', {
        'item': test,
        'item_type': 'test',
        'profile': profile,
        'price': test.price,
        'bank_accounts': bank_accounts,
    })


@login_required
def test_bundle_checkout(request, bundle_id):
    bundle = get_object_or_404(TestBundle, id=bundle_id)
    tests = bundle.tests.all()
    
    # Kiểm tra xem đã sở hữu trọn combo đề thi chưa
    owned_count = TestOwnership.objects.filter(user=request.user, test__in=tests).count()
    if tests.exists() and owned_count == tests.count():
        messages.info(request, "Bạn đã sở hữu toàn bộ đề thi trong gói này.")
        return redirect('test_bundle_detail', bundle_id=bundle.id)
        
    pending_tx = WalletTransaction.objects.filter(
        user=request.user,
        test_bundle=bundle,
        status='PENDING'
    ).first()
    if pending_tx:
        messages.warning(request, "Bạn đang có một yêu cầu thanh toán chuyển khoản chờ duyệt cho combo đề thi này.")
        return redirect('test_bundle_detail', bundle_id=bundle.id)
        
    profile = request.user.profile
    bank_accounts = BankAccount.objects.filter(is_active=True)
    
    if request.method == 'POST':
        payment_method = request.POST.get('payment_method')
        
        if payment_method == 'offline':
            proof_image = request.FILES.get('proof_image')
            bank_account_id = request.POST.get('bank_account_id')
            bank_account = None
            if bank_account_id:
                try:
                    bank_account = BankAccount.objects.get(id=bank_account_id, is_active=True)
                except BankAccount.DoesNotExist:
                    pass

            if not proof_image:
                messages.error(request, "Vui lòng tải lên ảnh chụp giao dịch / biên lai chuyển khoản.")
                return render(request, 'lms/checkout.html', {
                    'item': bundle,
                    'item_type': 'test_bundle',
                    'profile': profile,
                    'price': bundle.price,
                    'bank_accounts': bank_accounts,
                })
                
            WalletTransaction.objects.create(
                user=request.user,
                amount=bundle.price,
                transaction_type='PAYMENT',
                status='PENDING',
                proof_image=proof_image,
                test_bundle=bundle,
                bank_account=bank_account
            )
            messages.success(
                request, 
                "Gửi yêu cầu thanh toán thành công! Quyền làm bài sẽ được mở sau khi Admin duyệt biên lai của bạn."
            )
            return redirect('test_bundle_detail', bundle_id=bundle.id)
            
    return render(request, 'lms/checkout.html', {
        'item': bundle,
        'item_type': 'test_bundle',
        'profile': profile,
        'price': bundle.price,
        'bank_accounts': bank_accounts,
    })


@teacher_required
def teacher_courses_progress(request):
    """
    Dashboard liệt kê danh sách khóa học của giáo viên
    """
    if request.user.is_superuser:
        courses = Course.objects.all().order_by('-created_at')
    else:
        courses = Course.objects.filter(creator=request.user).order_by('-created_at')
        
    course_data = []
    for course in courses:
        student_count = CourseOwnership.objects.filter(course=course).count()
        lesson_count = course.lessons.count()
        course_data.append({
            'course': course,
            'student_count': student_count,
            'lesson_count': lesson_count,
        })
        
    return render(request, 'lms/teacher_courses_progress.html', {
        'course_data': course_data,
    })


@teacher_required
def teacher_course_detail_progress(request, course_id):
    """
    Chi tiết tiến độ học tập của từng học viên trong một khóa học cụ thể
    """
    course = get_object_or_404(Course, id=course_id)
    if not request.user.is_superuser and course.creator != request.user:
        messages.error(request, "Bạn không có quyền quản lý khóa học này.")
        return redirect('course_list')
        
    lessons = course.lessons.all().order_by('order_index')
    total_lessons = lessons.count()
    
    ownerships = CourseOwnership.objects.filter(course=course).select_related('user').order_by('-purchased_at')
    
    # 1. Áp dụng bộ lọc tìm kiếm theo tên/username học sinh
    student_query = request.GET.get('student', '').strip()
    if student_query:
        ownerships = ownerships.filter(
            Q(user__username__icontains=student_query) |
            Q(user__first_name__icontains=student_query) |
            Q(user__last_name__icontains=student_query)
        )
        
    # 2. Bulk query LessonProgress của tất cả học sinh này đối với các bài học này
    user_ids = [o.user_id for o in ownerships]
    lesson_ids = [l.id for l in lessons]
    
    progress_qs = LessonProgress.objects.filter(
        user_id__in=user_ids,
        lesson_id__in=lesson_ids,
        is_completed=True
    )
    
    # Tổ chức dữ liệu progress thành dictionary
    user_progress_dict = {}
    for p in progress_qs:
        if p.user_id not in user_progress_dict:
            user_progress_dict[p.user_id] = set()
        user_progress_dict[p.user_id].add(p.lesson_id)
        
    # 3. Tạo danh sách học sinh và áp dụng các bộ lọc tiến độ và thời hạn học
    student_data = []
    selected_progress = request.GET.get('progress', 'all')
    selected_status = request.GET.get('status', 'all')
    
    for ownership in ownerships:
        u = ownership.user
        completed_lessons = user_progress_dict.get(u.id, set())
        completed_count = len(completed_lessons)
        
        progress_percentage = 0
        if total_lessons > 0:
            progress_percentage = int((completed_count / total_lessons) * 100)
            
        # Lọc theo tiến độ
        if selected_progress == 'completed' and progress_percentage < 100:
            continue
        elif selected_progress == 'in_progress' and (progress_percentage == 0 or progress_percentage == 100):
            continue
        elif selected_progress == 'not_started' and progress_percentage > 0:
            continue
            
        # Lọc theo thời hạn
        is_expired = ownership.is_expired
        if selected_status == 'active' and is_expired:
            continue
        elif selected_status == 'expired' and not is_expired:
            continue
            
        # Chi tiết từng bài học của học viên này
        lesson_details = []
        for l in lessons:
            lesson_details.append({
                'id': l.id,
                'title': l.title,
                'lesson_type': l.get_lesson_type_display(),
                'is_completed': l.id in completed_lessons
            })
            
        student_data.append({
            'user': u,
            'ownership': ownership,
            'completed_count': completed_count,
            'progress_percentage': progress_percentage,
            'lesson_details': lesson_details,
            'is_expired': is_expired
        })
        
    return render(request, 'lms/teacher_course_detail_progress.html', {
        'course': course,
        'lessons': lessons,
        'total_lessons': total_lessons,
        'student_data': student_data,
        'student_query': student_query,
        'selected_progress': selected_progress,
        'selected_status': selected_status,
        'total_students_count': ownerships.count(),
    })


@login_required
def my_courses(request):
    from lms.models import CourseOwnership, LessonProgress, Course
    ownerships = CourseOwnership.objects.filter(user=request.user).select_related('course').order_by('-purchased_at')
    
    course_data = []
    owned_course_ids = set()
    for o in ownerships:
        course = o.course
        owned_course_ids.add(course.id)
        total_lessons = course.lessons.count()
        completed_lessons = LessonProgress.objects.filter(user=request.user, lesson__course=course, is_completed=True).count()
        progress_percent = int((completed_lessons / total_lessons * 100)) if total_lessons > 0 else 0
        
        course_data.append({
            'ownership': o,
            'course': course,
            'total_lessons': total_lessons,
            'completed_lessons': completed_lessons,
            'progress_percent': progress_percent,
            'is_expired': o.is_expired,
            'is_creator_trial': False,
        })
        
    # Bổ sung các khóa học tự tạo để giáo viên/admin dễ dàng vào học thử / xem thử
    if request.user.profile.can_create_courses or request.user.is_superuser or request.user.is_staff:
        created_courses = Course.objects.filter(creator=request.user).order_by('-id')
        for course in created_courses:
            if course.id not in owned_course_ids:
                total_lessons = course.lessons.count()
                completed_lessons = LessonProgress.objects.filter(user=request.user, lesson__course=course, is_completed=True).count()
                progress_percent = int((completed_lessons / total_lessons * 100)) if total_lessons > 0 else 0
                
                course_data.append({
                    'ownership': None,
                    'course': course,
                    'total_lessons': total_lessons,
                    'completed_lessons': completed_lessons,
                    'progress_percent': progress_percent,
                    'is_expired': False,
                    'is_creator_trial': True,
                })
        
    return render(request, 'lms/my_courses.html', {'courses': course_data})


@login_required
def my_tests(request):
    from lms.models import TestOwnership, Attempt
    ownerships = TestOwnership.objects.filter(user=request.user).select_related('test').order_by('-purchased_at')
    
    test_data = []
    for o in ownerships:
        test = o.test
        best_attempt = Attempt.objects.filter(user=request.user, test=test, end_time__isnull=False).order_by('-total_score').first()
        attempts_count = Attempt.objects.filter(user=request.user, test=test).count()
        
        test_data.append({
            'ownership': o,
            'test': test,
            'best_attempt': best_attempt,
            'attempts_count': attempts_count,
        })
        
    return render(request, 'lms/my_tests.html', {'tests': test_data})


@login_required
def update_attempt_visibility_ajax(request, attempt_id):
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404
    from lms.models import Attempt
    import json
    
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed.'}, status=405)
        
    attempt = get_object_or_404(Attempt, id=attempt_id, user=request.user)
    if attempt.end_time:
        return JsonResponse({'success': False, 'message': 'Attempt already completed.'})
        
    try:
        data = json.loads(request.body)
        left_page_count = int(data.get('left_page_count', 0))
        left_page_time = int(data.get('left_page_time', 0))
        
        attempt.left_page_count = left_page_count
        attempt.left_page_time = left_page_time
        attempt.save()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})








