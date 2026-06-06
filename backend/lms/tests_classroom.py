from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import User
from django.utils import timezone
from lms.models import (
    Course, Lesson, LessonProgress, MultiExerciseProgress,
    Test, Attempt, TestOwnership, Classroom, ClassroomMembership, ClassroomItem
)


class ClassroomBusinessLogicTests(TestCase):
    def setUp(self):
        # 1. Tạo giáo viên và học sinh
        self.teacher = User.objects.create_user(username="teacher_class", password="password")
        self.teacher.profile.can_create_courses = True
        self.teacher.profile.save()
        
        self.student = User.objects.create_user(username="student_class", password="password")
        
        # 2. Tạo khóa học và bài học có sẵn
        self.course = Course.objects.create(
            title="Khóa học C++",
            description="Lập trình C++",
            creator=self.teacher,
            price=50000
        )
        self.lesson_existing = Lesson.objects.create(
            course=self.course,
            title="Bài 1: Nhập môn",
            lesson_type="THEORY",
            order_index=1
        )
        
        # 3. Tạo đề thi có phí
        self.test_paid = Test.objects.create(
            title="Đề thi khảo sát",
            price=25000,
            duration=45,
            creator=self.teacher,
            test_type="STANDALONE"
        )
        self.lesson_test = Lesson.objects.create(
            course=self.course,
            title="Bài 2: Làm kiểm tra",
            lesson_type="TEST",
            order_index=2,
            test=self.test_paid
        )

    def test_classroom_creation_and_invite_flow(self):
        self.client.login(username="teacher_class", password="password")
        
        # 1. Tạo lớp học mới
        response = self.client.post(reverse('create_classroom'), {
            'name': 'Lớp chuyên Tin 11',
            'description': 'Luyện thi HSG'
        })
        self.assertEqual(response.status_code, 302)
        
        classroom = Classroom.objects.filter(name='Lớp chuyên Tin 11').first()
        self.assertIsNotNone(classroom)
        self.assertEqual(classroom.creator, self.teacher)
        # Kiểm tra tự động tạo khóa học nội bộ
        self.assertIsNotNone(classroom.course)
        self.assertEqual(classroom.course.title, "Lớp học: Lớp chuyên Tin 11")
        
        # 2. Học sinh truy cập link mời và gửi yêu cầu tham gia
        self.client.login(username="student_class", password="password")
        url_join = reverse('join_classroom_by_code', args=[classroom.invite_code])
        
        # Kiểm tra hiển thị trang giới thiệu
        response = self.client.get(url_join)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Lớp chuyên Tin 11")
        
        # Gửi POST yêu cầu tham gia
        response = self.client.post(url_join)
        self.assertEqual(response.status_code, 302)
        
        membership = ClassroomMembership.objects.filter(classroom=classroom, student=self.student).first()
        self.assertIsNotNone(membership)
        self.assertEqual(membership.status, 'PENDING')
        
        # 3. Giáo viên phê duyệt học sinh
        self.client.login(username="teacher_class", password="password")
        url_approve = reverse('classroom_membership_action', args=[membership.id, 'approve'])
        response = self.client.get(url_approve)
        self.assertEqual(response.status_code, 302)
        
        membership.refresh_from_db()
        self.assertEqual(membership.status, 'APPROVED')

    def test_classroom_content_assignment_and_student_access(self):
        # Tạo lớp học trước
        self.client.login(username="teacher_class", password="password")
        self.client.post(reverse('create_classroom'), {'name': 'Lớp C++', 'description': ''})
        classroom = Classroom.objects.get(name='Lớp C++')
        
        # 1. Thêm bài học có sẵn
        url_add_existing = reverse('classroom_add_existing_lesson', args=[classroom.id])
        response = self.client.post(url_add_existing, {'lesson_ids': [self.lesson_existing.id]})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ClassroomItem.objects.filter(classroom=classroom, lesson=self.lesson_existing).exists())
        
        # 2. Tạo bài học lý thuyết mới trực tiếp trong lớp
        url_create_lesson = reverse('classroom_create_lesson', args=[classroom.id])
        response = self.client.post(url_create_lesson, {
            'title': 'Bài lý thuyết lớp học',
            'lesson_type': 'THEORY',
            'content': 'Nội dung lý thuyết lớp học',
            'video_url': ''
        })
        self.assertEqual(response.status_code, 302)
        new_lesson = Lesson.objects.filter(title='Bài lý thuyết lớp học').first()
        self.assertIsNotNone(new_lesson)
        self.assertEqual(new_lesson.course, classroom.course)
        
        # 3. Cho học sinh xin vào lớp và được duyệt
        membership = ClassroomMembership.objects.create(classroom=classroom, student=self.student, status='APPROVED')
        
        # 4. Học sinh truy cập bài học trong lớp học
        self.client.login(username="student_class", password="password")
        url_lesson_detail = reverse('classroom_lesson_detail', args=[classroom.id, self.lesson_existing.id])
        response = self.client.get(url_lesson_detail)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bài 1: Nhập môn")
        
        # 5. Học sinh bấm hoàn thành bài học lý thuyết
        url_complete = reverse('classroom_complete_lesson_ajax', args=[classroom.id, self.lesson_existing.id])
        response = self.client.post(url_complete)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        
        # Kiểm tra LessonProgress
        self.assertTrue(LessonProgress.objects.filter(user=self.student, lesson=self.lesson_existing, is_completed=True).exists())

    def test_test_bypass_permissions_inside_classroom(self):
        # Tạo lớp học và duyệt học sinh
        classroom = Classroom.objects.create(
            name="Lớp thi cử",
            creator=self.teacher,
            course=Course.objects.create(title="Internal", creator=self.teacher)
        )
        ClassroomMembership.objects.create(classroom=classroom, student=self.student, status='APPROVED')
        
        # Giao bài học liên kết đề thi có phí vào lớp
        ClassroomItem.objects.create(classroom=classroom, lesson=self.lesson_test)
        
        # 1. Học sinh chưa mua đề thi này
        self.assertFalse(TestOwnership.objects.filter(user=self.student, test=self.test_paid).exists())
        
        # 2. Học sinh truy cập trang thi trực tiếp từ đề thi độc lập -> Bị chặn
        self.client.login(username="student_class", password="password")
        response = self.client.get(reverse('take_test', args=[self.test_paid.id]))
        self.assertEqual(response.status_code, 302) # Bị redirect về test_detail
        
        # 3. Học sinh truy cập bài học chứa đề thi đó TRONG LỚP HỌC -> Bỏ qua kiểm tra mua đề & Vào thi bình thường!
        # Trình duyệt truy cập trang chi tiết bài học của lớp học chứa đề thi
        response = self.client.get(reverse('classroom_lesson_detail', args=[classroom.id, self.lesson_test.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bắt đầu làm bài thi")
        
        # Truy cập API take_test có kèm lesson_id của lớp học -> Được phép truy cập làm bài (200 OK)
        response = self.client.get(f"{reverse('take_test', args=[self.test_paid.id])}?lesson_id={self.lesson_test.id}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Đề thi khảo sát")
