from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.http import JsonResponse, Http404
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Q
from django.utils import timezone
from django.contrib.auth.models import User

from .models import (
    Course, Lesson, LessonProgress, MultiExerciseProgress,
    Test, Attempt, TestOwnership, Classroom, ClassroomMembership, ClassroomItem
)
from .services import ScoringService, JudgeSyncService
from .views import extract_exercise_info_helper, parse_external_link_helper, teacher_required


@login_required
def classroom_list(request):
    """Xem danh sách các lớp học."""
    is_teacher = request.user.is_superuser or request.user.is_staff or getattr(request.user.profile, 'can_create_courses', False)
    
    created_classrooms = []
    joined_classrooms = []
    pending_classrooms = []
    
    if is_teacher:
        if request.user.is_superuser:
            created_classrooms = Classroom.objects.all().order_by('-created_at')
        else:
            created_classrooms = Classroom.objects.filter(creator=request.user).order_by('-created_at')
            
    # Lấy danh sách lớp đã tham gia/chờ duyệt đối với học sinh
    memberships = ClassroomMembership.objects.filter(student=request.user).select_related('classroom', 'classroom__creator')
    for m in memberships:
        if m.status == 'APPROVED':
            joined_classrooms.append(m.classroom)
        else:
            pending_classrooms.append(m.classroom)
            
    return render(request, 'lms/classroom_list.html', {
        'is_teacher': is_teacher,
        'created_classrooms': created_classrooms,
        'joined_classrooms': joined_classrooms,
        'pending_classrooms': pending_classrooms,
    })


@teacher_required
def create_classroom(request):
    """Tạo lớp học mới (tự động khởi tạo khóa học nội bộ ẩn)."""
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        
        if not name:
            messages.error(request, "Vui lòng nhập tên lớp học.")
            return redirect('create_classroom')
            
        with transaction.atomic():
            # 1. Tạo khóa học ẩn tương ứng cho lớp học
            internal_course = Course.objects.create(
                title=f"Lớp học: {name}",
                description=f"Khóa học chứa các bài học nội bộ của lớp {name}",
                price=0,
                learning_mode='FREE',
                creator=request.user
            )
            # 2. Tạo lớp học
            classroom = Classroom.objects.create(
                name=name,
                description=description,
                creator=request.user,
                course=internal_course
            )
            
        messages.success(request, f"Tạo lớp học '{name}' thành công.")
        return redirect('classroom_dashboard', classroom_id=classroom.id)
        
    return render(request, 'lms/create_classroom.html')


@login_required
def classroom_dashboard(request, classroom_id):
    """Trang chủ chi tiết của lớp học."""
    classroom = get_object_or_404(Classroom, id=classroom_id)
    
    is_creator = (classroom.creator == request.user or request.user.is_superuser)
    membership = ClassroomMembership.objects.filter(classroom=classroom, student=request.user).first()
    
    # Kiểm tra quyền truy cập lớp học
    if not is_creator:
        if not membership or membership.status != 'APPROVED':
            messages.error(request, "Bạn không có quyền truy cập lớp học này.")
            return redirect('classroom_list')
            
    # Lấy các bài học trong lớp học
    items = classroom.items.all().select_related('lesson', 'lesson__test').order_by('-added_at')
    
    if is_creator:
        # Giáo viên: Tính toán bảng ma trận tiến độ học sinh
        pending_members = classroom.memberships.filter(status='PENDING').select_related('student')
        approved_members = classroom.memberships.filter(status='APPROVED').select_related('student')
        
        student_progress = []
        for m in approved_members:
            student = m.student
            progress_row = {
                'student': student,
                'membership_id': m.id,
                'lessons': {}
            }
            
            for item in items:
                lesson = item.lesson
                if lesson.lesson_type == 'MULTI_EXERCISE':
                    # Đa bài tập: tính tỉ lệ hoàn thành
                    lines = [line.strip() for line in lesson.content.split('\n') if line.strip()]
                    total_links = 0
                    completed_links = 0
                    for line in lines:
                        url, platform, problem_code, is_hard = extract_exercise_info_helper(line)
                        if platform and problem_code:
                            total_links += 1
                            if MultiExerciseProgress.objects.filter(
                                user=student, lesson=lesson, link=url, is_completed=True
                            ).exists():
                                completed_links += 1
                    rate = int((completed_links / total_links) * 100) if total_links > 0 else 0
                    progress_row['lessons'][lesson.id] = {
                        'text': f"{completed_links}/{total_links}",
                        'percent': rate,
                        'is_completed': (rate == 100)
                    }
                else:
                    # Các loại bài lý thuyết / bài tập / đề thi: 100% nếu completed, 0% nếu chưa
                    completed = LessonProgress.objects.filter(user=student, lesson=lesson, is_completed=True).exists()
                    progress_row['lessons'][lesson.id] = {
                        'text': "Đã xong" if completed else "Chưa",
                        'percent': 100 if completed else 0,
                        'is_completed': completed
                    }
            student_progress.append(progress_row)
            
        # Xác định 5 bài mới nhất làm mặc định hiển thị
        latest_items = list(items[:5])
        latest_ids = [item.lesson.id for item in latest_items]
        
        return render(request, 'lms/classroom_dashboard.html', {
            'classroom': classroom,
            'is_teacher': True,
            'items': items,
            'pending_members': pending_members,
            'approved_members': approved_members,
            'student_progress': student_progress,
            'latest_ids': latest_ids,
        })
        
    else:
        # Học sinh: Tính trạng thái hoàn thành bài học của học sinh hiện tại
        timeline = []
        for item in items:
            lesson = item.lesson
            status = {
                'item': item,
                'is_completed': False,
                'progress_text': ''
            }
            
            if lesson.lesson_type == 'MULTI_EXERCISE':
                lines = [line.strip() for line in lesson.content.split('\n') if line.strip()]
                total_links = 0
                completed_links = 0
                for line in lines:
                    url, platform, problem_code, is_hard = extract_exercise_info_helper(line)
                    if platform and problem_code:
                        total_links += 1
                        if MultiExerciseProgress.objects.filter(
                            user=request.user, lesson=lesson, link=url, is_completed=True
                        ).exists():
                            completed_links += 1
                status['is_completed'] = (total_links > 0 and completed_links == total_links)
                status['progress_text'] = f"{completed_links}/{total_links} bài nộp AC"
            else:
                completed = LessonProgress.objects.filter(user=request.user, lesson=lesson, is_completed=True).exists()
                status['is_completed'] = completed
                status['progress_text'] = "Đã hoàn thành" if completed else "Chưa hoàn thành"
                
            timeline.append(status)
            
        # Lấy danh sách bạn cùng lớp
        classmates = classroom.memberships.filter(status='APPROVED').select_related('student').exclude(student=request.user)
        
        return render(request, 'lms/classroom_dashboard.html', {
            'classroom': classroom,
            'is_teacher': False,
            'timeline': timeline,
            'classmates': classmates,
        })


@login_required
def join_classroom_by_code(request, invite_code):
    """Trang trung gian khi học sinh bấm link mời tham gia lớp học."""
    classroom = get_object_or_404(Classroom, invite_code=invite_code)
    
    # Giáo viên của lớp không cần xin tham gia lớp của chính mình
    if classroom.creator == request.user or request.user.is_superuser:
        return redirect('classroom_dashboard', classroom_id=classroom.id)
        
    membership = ClassroomMembership.objects.filter(classroom=classroom, student=request.user).first()
    
    if request.method == 'POST':
        if not membership:
            ClassroomMembership.objects.create(
                classroom=classroom,
                student=request.user,
                status='PENDING'
            )
            messages.success(request, "Đã gửi yêu cầu tham gia lớp học. Vui lòng chờ giáo viên phê duyệt.")
        return redirect('classroom_list')
        
    return render(request, 'lms/classroom_join.html', {
        'classroom': classroom,
        'membership': membership,
    })


@teacher_required
def classroom_membership_action(request, membership_id, action):
    """Phê duyệt, từ chối yêu cầu tham gia hoặc xóa học sinh khỏi lớp học."""
    membership = get_object_or_404(ClassroomMembership, id=membership_id)
    classroom = membership.classroom
    
    if classroom.creator != request.user and not request.user.is_superuser:
        messages.error(request, "Bạn không có quyền quản lý thành viên của lớp học này.")
        return redirect('classroom_list')
        
    if action == 'approve':
        membership.status = 'APPROVED'
        membership.save()
        messages.success(request, f"Đã duyệt học sinh {membership.student.username} vào lớp.")
    elif action == 'reject' or action == 'remove':
        student_name = membership.student.username
        membership.delete()
        messages.success(request, f"Đã từ chối/xóa học sinh {student_name} khỏi lớp.")
        
    # Cho phép giáo viên thêm trực tiếp học sinh bằng form POST từ dashboard
    if action == 'add_direct' and request.method == 'POST':
        search_query = request.POST.get('student_identifier', '').strip()
        student_user = User.objects.filter(Q(username=search_query) | Q(email=search_query)).first()
        
        if not student_user:
            messages.error(request, f"Không tìm thấy học sinh với thông tin: '{search_query}'.")
        else:
            # Kiểm tra xem đã là thành viên chưa
            exist_m = ClassroomMembership.objects.filter(classroom=classroom, student=student_user).first()
            if exist_m:
                if exist_m.status == 'APPROVED':
                    messages.warning(request, f"Học sinh {student_user.username} đã ở trong lớp từ trước.")
                else:
                    exist_m.status = 'APPROVED'
                    exist_m.save()
                    messages.success(request, f"Đã duyệt yêu cầu chờ của học sinh {student_user.username}.")
            else:
                ClassroomMembership.objects.create(
                    classroom=classroom,
                    student=student_user,
                    status='APPROVED'
                )
                messages.success(request, f"Đã thêm trực tiếp học sinh {student_user.username} vào lớp học.")
                
    return redirect('classroom_dashboard', classroom_id=classroom.id)


@teacher_required
def classroom_add_existing_lesson(request, classroom_id):
    """Giáo viên chọn bài học từ các khóa học hiện tại để giao cho lớp."""
    classroom = get_object_or_404(Classroom, id=classroom_id)
    
    if classroom.creator != request.user and not request.user.is_superuser:
        messages.error(request, "Bạn không có quyền chỉnh sửa lớp học này.")
        return redirect('classroom_list')
        
    if request.method == 'POST':
        lesson_ids = request.POST.getlist('lesson_ids')
        if not lesson_ids:
            messages.error(request, "Vui lòng chọn ít nhất một bài học để giao.")
            return redirect('classroom_add_existing_lesson', classroom_id=classroom.id)
            
        added_count = 0
        for l_id in lesson_ids:
            lesson = Lesson.objects.filter(id=l_id).first()
            if lesson:
                # Kiểm tra xem bài học đã được thêm vào lớp chưa
                if not ClassroomItem.objects.filter(classroom=classroom, lesson=lesson).exists():
                    ClassroomItem.objects.create(classroom=classroom, lesson=lesson)
                    added_count += 1
                    
        messages.success(request, f"Đã giao thành công {added_count} bài học vào lớp.")
        return redirect('classroom_dashboard', classroom_id=classroom.id)
        
    # Lấy danh sách các bài học của giáo viên tạo ra
    if request.user.is_superuser:
        courses = Course.objects.all().prefetch_related('lessons')
    else:
        courses = Course.objects.filter(creator=request.user).prefetch_related('lessons')
        
    # Loại trừ các khóa học nội bộ của lớp
    courses = courses.exclude(classroom_course__isnull=False)
    
    # Lấy danh sách ID các bài học đã giao trong lớp để đánh dấu checkbox
    assigned_lesson_ids = set(classroom.items.values_list('lesson_id', flat=True))
    
    return render(request, 'lms/classroom_add_existing.html', {
        'classroom': classroom,
        'courses': courses,
        'assigned_lesson_ids': assigned_lesson_ids,
    })


@teacher_required
def classroom_create_lesson(request, classroom_id):
    """Giáo viên biên soạn/tạo một bài học mới trực tiếp cho lớp học."""
    classroom = get_object_or_404(Classroom, id=classroom_id)
    
    if classroom.creator != request.user and not request.user.is_superuser:
        messages.error(request, "Bạn không có quyền chỉnh sửa lớp học này.")
        return redirect('classroom_list')
        
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        lesson_type = request.POST.get('lesson_type', 'THEORY')
        content = request.POST.get('content', '').strip()
        video_url = request.POST.get('video_url', '').strip()
        external_judge_link = request.POST.get('external_judge_link', '').strip()
        external_problem_code = request.POST.get('external_problem_code', '').strip()
        test_id = request.POST.get('test_id', '')
        
        if not title:
            messages.error(request, "Vui lòng nhập tiêu đề bài học.")
            return redirect('classroom_create_lesson', classroom_id=classroom.id)
            
        test = None
        if lesson_type == 'TEST' and test_id:
            test = get_object_or_404(Test, id=test_id)
            
        with transaction.atomic():
            # Xác định order_index tiếp theo của khóa học nội bộ lớp học
            max_order = classroom.course.lessons.all().count() + 1
            
            # Tạo lesson
            new_lesson = Lesson.objects.create(
                course=classroom.course,
                title=title,
                lesson_type=lesson_type,
                content=content,
                video_url=video_url,
                order_index=max_order,
                external_judge_link=external_judge_link,
                external_problem_code=external_problem_code,
                test=test
            )
            # Tạo ClassroomItem giao bài
            ClassroomItem.objects.create(
                classroom=classroom,
                lesson=new_lesson
            )
            
        messages.success(request, f"Tạo bài học mới '{title}' thành công và đã giao cho lớp.")
        return redirect('classroom_dashboard', classroom_id=classroom.id)
        
    tests = Test.objects.all().order_by('-id')
    return render(request, 'lms/classroom_create_lesson.html', {
        'classroom': classroom,
        'tests': tests,
    })


@login_required
def classroom_lesson_detail(request, classroom_id, lesson_id):
    """Xem bài học và học bài trong ngữ cảnh lớp học."""
    classroom = get_object_or_404(Classroom, id=classroom_id)
    
    # Check permissions
    is_creator = (classroom.creator == request.user or request.user.is_superuser)
    membership = ClassroomMembership.objects.filter(classroom=classroom, student=request.user).first()
    if not is_creator and (not membership or membership.status != 'APPROVED'):
        messages.error(request, "Bạn không có quyền truy cập bài học trong lớp này.")
        return redirect('classroom_list')
        
    lesson = get_object_or_404(Lesson, id=lesson_id)
    
    # Kiểm tra xem bài học này có nằm trong lớp học này không
    if not ClassroomItem.objects.filter(classroom=classroom, lesson=lesson).exists():
        raise Http404("Bài học này chưa được giao trong lớp học này.")
        
    # Lấy toàn bộ danh sách bài học của lớp học theo dòng thời gian phục vụ sidebar
    items = classroom.items.all().select_related('lesson').order_by('added_at')
    
    lessons_status = []
    for item in items:
        l = item.lesson
        is_current = (l.id == lesson.id)
        is_completed = LessonProgress.objects.filter(user=request.user, lesson=l, is_completed=True).exists()
        
        # Với lớp học, mặc định các bài được hiển thị mở khóa hoàn toàn tự do (learning_mode='FREE')
        lessons_status.append({
            'lesson': l,
            'is_current': is_current,
            'is_completed': is_completed,
            'is_locked': False
        })
        
    # Xem kết quả nộp bài thi luyện tập / chính thức của đề thi liên kết
    attempt_info = None
    if lesson.lesson_type == 'TEST' and lesson.test:
        active_attempt = Attempt.objects.filter(user=request.user, test=lesson.test, end_time__isnull=True).first()
        past_attempts = Attempt.objects.filter(user=request.user, test=lesson.test, end_time__isnull=False).order_by('-start_time')
        attempt_info = {
            'active_attempt': active_attempt,
            'past_attempts': past_attempts
        }
        
    # Trích xuất dải link đa bài tập Judge ngoại vi
    multi_exercises = []
    if lesson.lesson_type == 'MULTI_EXERCISE':
        lines = [line.strip() for line in lesson.content.split('\n') if line.strip()]
        for line in lines:
            url, platform, problem_code, is_hard = extract_exercise_info_helper(line)
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
                
    return render(request, 'lms/classroom_lesson_detail.html', {
        'classroom': classroom,
        'lesson': lesson,
        'lessons_status': lessons_status,
        'attempt_info': attempt_info,
        'multi_exercises': multi_exercises,
    })


@login_required
def classroom_complete_lesson_ajax(request, classroom_id, lesson_id):
    """Hoàn thành bài học bằng AJAX trong ngữ cảnh lớp học."""
    classroom = get_object_or_404(Classroom, id=classroom_id)
    membership = ClassroomMembership.objects.filter(classroom=classroom, student=request.user).first()
    is_creator = (classroom.creator == request.user or request.user.is_superuser)
    
    if not is_creator and (not membership or membership.status != 'APPROVED'):
        return JsonResponse({'success': False, 'message': 'Không có quyền truy cập lớp học.'}, status=403)
        
    lesson = get_object_or_404(Lesson, id=lesson_id)
    if not ClassroomItem.objects.filter(classroom=classroom, lesson=lesson).exists():
        return JsonResponse({'success': False, 'message': 'Bài học không thuộc lớp.'}, status=404)
        
    if not is_creator:
        LessonProgress.objects.update_or_create(
            user=request.user,
            lesson=lesson,
            defaults={'is_completed': True, 'completed_at': timezone.now()}
        )
        
    # Tìm bài học tiếp theo trong lớp học
    items = list(classroom.items.all().order_by('added_at'))
    next_url = None
    for idx, item in enumerate(items):
        if item.lesson.id == lesson.id and idx + 1 < len(items):
            next_url = f"/classrooms/{classroom.id}/lessons/{items[idx+1].lesson.id}/"
            break
            
    return JsonResponse({
        'success': True,
        'next_url': next_url
    })


@login_required
def classroom_sync_lesson_progress_ajax(request, classroom_id, lesson_id):
    """Đồng bộ hóa kết quả nộp bài tập đơn lẻ trên Judge của học sinh lớp học."""
    classroom = get_object_or_404(Classroom, id=classroom_id)
    membership = ClassroomMembership.objects.filter(classroom=classroom, student=request.user).first()
    is_creator = (classroom.creator == request.user or request.user.is_superuser)
    
    if not is_creator and (not membership or membership.status != 'APPROVED'):
        return JsonResponse({'success': False, 'message': 'Không có quyền truy cập.'}, status=403)
        
    lesson = get_object_or_404(Lesson, id=lesson_id)
    if not ClassroomItem.objects.filter(classroom=classroom, lesson=lesson).exists():
        return JsonResponse({'success': False, 'message': 'Bài học không thuộc lớp.'}, status=404)
        
    if lesson.lesson_type != 'EXERCISE' or not lesson.external_problem_code:
        return JsonResponse({'success': False, 'message': 'Bài học không liên kết bài tập ngoại vi.'}, status=400)
        
    profile = request.user.profile
    if not profile.codeforces_username and not profile.vnoj_username and not profile.dmoj_username:
        return JsonResponse({'success': False, 'message': 'Vui lòng cập nhật username VNOJ, Codeforces hoặc on.hsgtin.vn trong trang cá nhân.'}, status=400)
        
    success = False
    message = "Không tìm thấy bài nộp AC nào cho bài tập này."
    link_lower = lesson.external_judge_link.lower() if lesson.external_judge_link else ""
    
    try:
        if 'codeforces' in link_lower and profile.codeforces_username:
            success = JudgeSyncService.sync_codeforces(profile.codeforces_username, lesson.external_problem_code)
        elif 'vnoj' in link_lower and profile.vnoj_username:
            success = JudgeSyncService.sync_vnoj(profile.vnoj_username, lesson.external_problem_code)
        elif ('hsgtin' in link_lower or 'on.hsgtin.vn' in link_lower) and profile.dmoj_username:
            success = JudgeSyncService.sync_dmoj(profile.dmoj_username, lesson.external_problem_code)
            
        if success:
            if not is_creator:
                LessonProgress.objects.update_or_create(
                    user=request.user,
                    lesson=lesson,
                    defaults={'is_completed': True, 'completed_at': timezone.now()}
                )
            return JsonResponse({'success': True, 'message': 'Đồng bộ kết quả thành công! Bạn đã hoàn thành bài tập.'})
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'Lỗi đồng bộ: {str(e)}'}, status=500)
        
    return JsonResponse({'success': False, 'message': message})


@login_required
def classroom_sync_multi_exercise_ajax(request, classroom_id, lesson_id):
    """Đồng bộ hóa một bài tập cụ thể trong chuỗi Đa bài tập của lớp học."""
    classroom = get_object_or_404(Classroom, id=classroom_id)
    membership = ClassroomMembership.objects.filter(classroom=classroom, student=request.user).first()
    is_creator = (classroom.creator == request.user or request.user.is_superuser)
    
    if not is_creator and (not membership or membership.status != 'APPROVED'):
        return JsonResponse({'success': False, 'message': 'Không có quyền truy cập.'}, status=403)
        
    lesson = get_object_or_404(Lesson, id=lesson_id)
    if not ClassroomItem.objects.filter(classroom=classroom, lesson=lesson).exists():
        return JsonResponse({'success': False, 'message': 'Bài học không thuộc lớp.'}, status=404)
        
    if lesson.lesson_type != 'MULTI_EXERCISE':
        return JsonResponse({'success': False, 'message': 'Không phải loại đa bài tập.'}, status=400)
        
    import json
    try:
        data = json.loads(request.body)
        link = data.get('link', '').strip()
    except Exception:
        return JsonResponse({'success': False, 'message': 'Dữ liệu yêu cầu không hợp lệ.'}, status=400)
        
    if not link:
        return JsonResponse({'success': False, 'message': 'Thiếu đường dẫn bài tập cần đồng bộ.'}, status=400)
        
    platform, problem_code = parse_external_link_helper(link)
    if not platform or not problem_code:
        return JsonResponse({'success': False, 'message': 'Không nhận diện được hệ thống Judge hoặc mã bài tập.'}, status=400)
        
    profile = request.user.profile
    success = False
    
    try:
        if platform == 'codeforces' and profile.codeforces_username:
            success = JudgeSyncService.sync_codeforces(profile.codeforces_username, problem_code)
        elif platform == 'vnoj' and profile.vnoj_username:
            success = JudgeSyncService.sync_vnoj(profile.vnoj_username, problem_code)
        elif platform == 'hsgtin' and profile.dmoj_username:
            success = JudgeSyncService.sync_dmoj(profile.dmoj_username, problem_code)
            
        if success:
            if not is_creator:
                MultiExerciseProgress.objects.update_or_create(
                    user=request.user,
                    lesson=lesson,
                    link=link,
                    defaults={'is_completed': True, 'completed_at': timezone.now()}
                )
                
                # Check if all exercises are completed to auto-complete the main lesson
                lines = [line.strip() for line in lesson.content.split('\n') if line.strip()]
                total_links = 0
                completed_links = 0
                for line in lines:
                    url, plat, code, is_hard = extract_exercise_info_helper(line)
                    if plat and code:
                        total_links += 1
                        if MultiExerciseProgress.objects.filter(
                            user=request.user, lesson=lesson, link=url, is_completed=True
                        ).exists():
                            completed_links += 1
                            
                if total_links > 0 and completed_links == total_links:
                    LessonProgress.objects.update_or_create(
                        user=request.user,
                        lesson=lesson,
                        defaults={'is_completed': True, 'completed_at': timezone.now()}
                    )
            return JsonResponse({'success': True, 'message': f'Đồng bộ bài {problem_code} thành công.'})
            
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'Lỗi đồng bộ: {str(e)}'}, status=500)
        
    return JsonResponse({'success': False, 'message': f'Không tìm thấy bài nộp AC cho bài {problem_code} trên {platform.upper()}.'})
