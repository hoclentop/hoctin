from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils import timezone
from lms.models import (
    DynamicTest, QuestionGroup, Question, Choice, Attempt, AttemptAnswer,
    Course, Lesson, LessonProgress, CourseOwnership, WalletTransaction, Test
)
from lms.services import ScoringService

class DynamicTestTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='student', password='password123')
        self.creator = User.objects.create_superuser(username='admin', password='adminpassword')
        
        # Create Question Groups
        self.group1 = QuestionGroup.objects.create(name='Group 1')
        self.group2 = QuestionGroup.objects.create(name='Group 2')
        
        # Create Questions in Group 1
        self.q1 = Question.objects.create(
            content='Question 1',
            question_type=1, # Single Choice
            group=self.group1
        )
        self.c1_correct = Choice.objects.create(question=self.q1, content='Choice 1 Correct', is_correct=True, position=1)
        self.c1_incorrect = Choice.objects.create(question=self.q1, content='Choice 1 Incorrect', is_correct=False, position=2)
        
        self.q2 = Question.objects.create(
            content='Question 2',
            question_type=1,
            group=self.group1
        )
        self.c2_correct = Choice.objects.create(question=self.q2, content='Choice 2 Correct', is_correct=True, position=1)
        
        # Create Question in Group 2
        self.q3 = Question.objects.create(
            content='Question 3',
            question_type=1,
            group=self.group2
        )
        self.c3_correct = Choice.objects.create(question=self.q3, content='Choice 3 Correct', is_correct=True, position=1)
        
        # Create a question not in any group (to test explicit ID)
        self.q4 = Question.objects.create(
            content='Question 4',
            question_type=1
        )
        self.c4_correct = Choice.objects.create(question=self.q4, content='Choice 4 Correct', is_correct=True, position=1)

    def test_dynamic_test_generation_and_scoring(self):
        # 1. Create a DynamicTest
        # Rules: select 2 from group 1 (ID self.group1.id), select 1 from group 2 (ID self.group2.id)
        # Explicit IDs: select self.q4.id
        # Max questions = 3 (this will trigger dropping/trimming because 2 + 1 + 1 = 4 questions)
        # Total points = 10.0
        dt = DynamicTest.objects.create(
            title='Dynamic Test 1',
            price=0.0,
            max_questions=3,
            total_points=10.0,
            dynamic_group_rules=f"{self.group1.id}:2, {self.group2.id}:1",
            dynamic_question_ids=f"{self.q4.id}",
            creator=self.creator
        )
        
        # 2. Start an attempt
        # Client login
        self.client.login(username='student', password='password123')
        
        # We call the take_test view
        from django.urls import reverse
        response = self.client.get(reverse('take_test', args=[dt.id]))
        self.assertEqual(response.status_code, 200)
        
        attempt = Attempt.objects.get(user=self.user, test=dt)
        self.assertIsNotNone(attempt.shuffled_data)
        
        # Verify attempt.shuffled_data structure and question count
        # In shuffled_data, key is "1" (the only part)
        questions_in_attempt = attempt.shuffled_data["1"]
        self.assertEqual(len(questions_in_attempt), 3) # limited to max_questions
        
        # The points for each question should be 10.0 / 3 = 3.33 (approx)
        expected_points = round(10.0 / 3, 2)
        for q_info in questions_in_attempt:
            self.assertEqual(q_info['points'], expected_points)
            
        # Verify that explicit question self.q4.id is kept (since it has higher priority)
        selected_question_ids = [q_info['question_id'] for q_info in questions_in_attempt]
        self.assertIn(self.q4.id, selected_question_ids)
        
        # 3. Submit Answers
        # We will build POST data. Since we shuffled choices, let's look up choices from database
        # And construct answers
        post_data = {}
        for q_info in questions_in_attempt:
            q_id = q_info['question_id']
            key = f"q_{q_id}"
            if q_id == self.q4.id:
                # Answer correctly
                post_data[key] = [self.c4_correct.id]
            elif q_id == self.q1.id:
                # Answer incorrectly
                post_data[key] = [self.c1_incorrect.id]
            elif q_id == self.q2.id:
                # Answer correctly
                post_data[key] = [self.c2_correct.id]
            elif q_id == self.q3.id:
                # Answer correctly
                post_data[key] = [self.c3_correct.id]
                
        # Post submit
        response = self.client.post(reverse('submit_test', args=[attempt.id]), post_data)
        self.assertEqual(response.status_code, 302) # redirect to test_result
        
        # 4. Check scoring
        attempt.refresh_from_db()
        self.assertIsNotNone(attempt.end_time)
        
        # We have 3 questions in total.
        # We answered 2 correctly and 1 incorrectly (or similar depending on which questions were selected).
        # Let's count how many we answered correctly
        correct_count = 0
        for q_info in questions_in_attempt:
            q_id = q_info['question_id']
            ans = AttemptAnswer.objects.get(attempt=attempt, question_id=q_id)
            if q_id == self.q4.id and self.c4_correct.id in ans.selected_choices.values_list('id', flat=True):
                correct_count += 1
            elif q_id == self.q2.id and self.c2_correct.id in ans.selected_choices.values_list('id', flat=True):
                correct_count += 1
            elif q_id == self.q3.id and self.c3_correct.id in ans.selected_choices.values_list('id', flat=True):
                correct_count += 1
                
        expected_score = correct_count * expected_points
        self.assertAlmostEqual(attempt.total_score, expected_score, places=2)
        
        # 5. Check review view
        response = self.client.get(reverse('review_attempt', args=[attempt.id]))
        self.assertEqual(response.status_code, 200)

    def test_equivalent_group_exclusion_in_dynamic_test(self):
        # Let's create an EquivalentQuestionGroup
        from lms.models import EquivalentQuestionGroup
        eqg = EquivalentQuestionGroup.objects.create(name='Equivalent Group 1')
        
        # Link q1 and q2 to this equivalent group
        self.q1.equivalent_group = eqg
        self.q1.save()
        self.q2.equivalent_group = eqg
        self.q2.save()
        
        # Create a DynamicTest that requests 2 questions from group1 (which has q1 and q2)
        dt = DynamicTest.objects.create(
            title='Dynamic Test Equivalent Check',
            price=0.0,
            max_questions=10,
            total_points=10.0,
            dynamic_group_rules=f"{self.group1.id}:2",
            creator=self.creator
        )
        
        # Start an attempt
        self.client.login(username='student', password='password123')
        from django.urls import reverse
        response = self.client.get(reverse('take_test', args=[dt.id]))
        self.assertEqual(response.status_code, 200)
        
        attempt = Attempt.objects.get(user=self.user, test=dt)
        questions_in_attempt = attempt.shuffled_data["1"]
        
        # Even though the rule asked for 2 questions, because q1 and q2 are equivalent,
        # only 1 question should be selected in the end!
        self.assertEqual(len(questions_in_attempt), 1)

class CourseAndLessonTestCase(TestCase):
    def setUp(self):
        # Create users
        self.admin = User.objects.create_superuser(username='admin', password='adminpassword')
        self.student = User.objects.create_user(username='student1', password='password123')
        self.teacher = User.objects.create_user(username='thnam', password='password123')
        
        # Grant teacher role
        self.teacher.profile.can_create_courses = True
        self.teacher.profile.save()
        
        # Create a course
        self.free_course = Course.objects.create(
            title='Python Free Course',
            description='Free introduction to Python.',
            price=0.0,
            learning_mode='SEQUENTIAL',
            creator=self.teacher
        )
        
        # Create a paid course
        self.paid_course = Course.objects.create(
            title='Advanced Python Course',
            description='Advanced Python concepts.',
            price=50000.0,
            learning_mode='FREE',
            creator=self.teacher
        )

        # Create lessons for sequential course
        self.lesson1 = Lesson.objects.create(
            title='Lesson 1: Intro',
            course=self.free_course,
            lesson_type='THEORY',
            content='Intro content.',
            order_index=1
        )
        
        self.lesson2 = Lesson.objects.create(
            title='Lesson 2: Variables',
            course=self.free_course,
            lesson_type='THEORY',
            content='Variables content.',
            order_index=2
        )

    def test_course_access_permissions(self):
        self.client.login(username='student1', password='password123')
        from django.urls import reverse
        
        # Student cannot create course
        response = self.client.post(reverse('create_course'), {
            'title': 'New Course',
            'description': 'Desc',
            'price': 0,
            'learning_mode': 'FREE'
        })
        self.assertEqual(response.status_code, 302) # redirected with error
        
        # Teacher can create course
        self.client.login(username='thnam', password='password123')
        response = self.client.post(reverse('create_course'), {
            'title': 'New Course By Teacher',
            'description': 'Desc',
            'price': 0,
            'learning_mode': 'FREE'
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Course.objects.filter(title='New Course By Teacher').exists())

    def test_sequential_learning_flow(self):
        from django.urls import reverse
        # Student logs in
        self.client.login(username='student1', password='password123')
        
        # Must register/buy first
        response = self.client.get(reverse('lesson_detail', args=[self.free_course.id, self.lesson1.id]))
        self.assertEqual(response.status_code, 302) # Redirect to course detail to enroll
        
        # Buy/enroll free course
        response = self.client.post(reverse('buy_course', args=[self.free_course.id]))
        self.assertEqual(response.status_code, 302)
        
        # Now can access lesson 1
        response = self.client.get(reverse('lesson_detail', args=[self.free_course.id, self.lesson1.id]))
        self.assertEqual(response.status_code, 200)
        
        # Cannot access lesson 2 yet (sequential locking)
        response = self.client.get(reverse('lesson_detail', args=[self.free_course.id, self.lesson2.id]))
        self.assertEqual(response.status_code, 302) # Redirected back to lesson 1
        
        # Complete lesson 1
        response = self.client.post(reverse('complete_lesson_ajax', args=[self.lesson1.id]))
        self.assertEqual(response.status_code, 200)
        
        # Now can access lesson 2
        response = self.client.get(reverse('lesson_detail', args=[self.free_course.id, self.lesson2.id]))
        self.assertEqual(response.status_code, 200)

    def test_paid_course_purchase_logic(self):
        from django.urls import reverse
        self.client.login(username='student1', password='password123')
        
        # Try to buy without enough money
        response = self.client.post(reverse('buy_course', args=[self.paid_course.id]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CourseOwnership.objects.filter(user=self.student, course=self.paid_course).exists())
        
        # Deposit money
        profile = self.student.profile
        profile.wallet_balance = 100000.0
        profile.save()
        
        # Buy again
        response = self.client.post(reverse('buy_course', args=[self.paid_course.id]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(CourseOwnership.objects.filter(user=self.student, course=self.paid_course).exists())
        
        # Check wallet deduction
        profile.refresh_from_db()
        self.assertEqual(profile.wallet_balance, 50000.0)

    def test_test_lesson_autocompletion_on_submit(self):
        from django.urls import reverse
        from lms.models import Test, LessonProgress
        
        # Create a test
        test_obj = Test.objects.create(
            title='Lesson Test',
            price=0.0,
            duration=30,
            creator=self.admin
        )
        
        # Create a lesson linked to this test
        test_lesson = Lesson.objects.create(
            title='Practice Test Lesson',
            course=self.free_course,
            lesson_type='TEST',
            test=test_obj,
            order_index=3
        )
        
        # Register student in the free course
        CourseOwnership.objects.create(user=self.student, course=self.free_course)
        
        # Create a mock attempt
        attempt = Attempt.objects.create(
            user=self.student,
            test=test_obj,
            start_time=timezone.now()
        )
        
        # Submit the attempt (POST)
        self.client.login(username='student1', password='password123')
        response = self.client.post(reverse('submit_test', args=[attempt.id]), {})
        self.assertEqual(response.status_code, 302)
        
        # Check if the linked lesson is automatically marked as completed
        is_completed = LessonProgress.objects.filter(user=self.student, lesson=test_lesson, is_completed=True).exists()
        self.assertTrue(is_completed)

class ProfileTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='password123')
        
    def test_profile_edit_requires_login(self):
        from django.urls import reverse
        # Non-logged in users should be redirected to login page
        response = self.client.get(reverse('edit_profile'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)
        
    def test_profile_edit_get_and_post(self):
        from django.urls import reverse
        self.client.login(username='tester', password='password123')
        
        # Test GET
        response = self.client.get(reverse('edit_profile'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'lms/edit_profile.html')
        
        # Test POST update details
        post_data = {
            'first_name': 'Nguyen',
            'last_name': 'Van A',
            'email': 'vana@example.com',
            'vnoj_username': 'vnoj_user',
            'codeforces_username': 'cf_user',
            'dmoj_username': 'dmoj_user',
        }
        response = self.client.post(reverse('edit_profile'), post_data)
        self.assertEqual(response.status_code, 302) # redirects to edit_profile on success
        
        # Verify persistence in DB
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'Nguyen')
        self.assertEqual(self.user.last_name, 'Van A')
        self.assertEqual(self.user.email, 'vana@example.com')
        
        profile = self.user.profile
        self.assertEqual(profile.vnoj_username, 'vnoj_user')
        self.assertEqual(profile.codeforces_username, 'cf_user')
        self.assertEqual(profile.dmoj_username, 'dmoj_user')

from unittest.mock import patch

class JudgeSyncTestCase(TestCase):
    def test_sync_dmoj_platform_success(self):
        from lms.services import JudgeSyncService
        
        sample_html = """
        <div id="submissions-table">
            <div class="submission-row" id="131729">
                <div class="sub-result AC">
                    <span class="status" title="Kết quả đúng (AC)">AC</span>
                </div>
                <div class="sub-main">
                    <div class="sub-info">
                        <div class="name">
                            <a href="/problem/hsg7d25b4">D254 Số nguyên tố cùng nhau</a>
                        </div>
                        <span class="rating rate-none user"><a href="/user/dangkhoa">dangkhoa</a></span>
                    </div>
                </div>
            </div>
        </div>
        """
        
        class MockResponse:
            def __init__(self, text, status_code):
                self.text = text
                self.status_code = status_code
                
        with patch('requests.get') as mock_get:
            mock_get.return_value = MockResponse(sample_html, 200)
            
            # Test success case
            success = JudgeSyncService._sync_dmoj_platform("http://on.hsgtin.vn", "dangkhoa", "hsg7d25b4")
            self.assertTrue(success)
            mock_get.assert_called_with("http://on.hsgtin.vn/submissions/user/dangkhoa/?status=AC", timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            
            # Test case-insensitive match on username and problem code
            success_case = JudgeSyncService._sync_dmoj_platform("http://on.hsgtin.vn", "DangKhoa", "HSG7D25B4")
            self.assertTrue(success_case)
            
            # Test incorrect problem code
            fail_prob = JudgeSyncService._sync_dmoj_platform("http://on.hsgtin.vn", "dangkhoa", "hsg7d9999")
            self.assertFalse(fail_prob)
            
            # Test incorrect user
            fail_user = JudgeSyncService._sync_dmoj_platform("http://on.hsgtin.vn", "other_user", "hsg7d25b4")
            self.assertFalse(fail_user)
            
    def test_sync_dmoj_platform_non_ac(self):
        from lms.services import JudgeSyncService
        
        sample_html_non_ac = """
        <div id="submissions-table">
            <div class="submission-row" id="131729">
                <div class="sub-result WA">
                    <span class="status" title="Kết quả sai (WA)">WA</span>
                </div>
                <div class="sub-main">
                    <div class="sub-info">
                        <div class="name">
                            <a href="/problem/hsg7d25b4">D254</a>
                        </div>
                        <span class="rating rate-none user"><a href="/user/dangkhoa">dangkhoa</a></span>
                    </div>
                </div>
            </div>
        </div>
        """
        class MockResponse:
            def __init__(self, text, status_code):
                self.text = text
                self.status_code = status_code
                
        with patch('requests.get') as mock_get:
            mock_get.return_value = MockResponse(sample_html_non_ac, 200)
            
            success = JudgeSyncService._sync_dmoj_platform("http://on.hsgtin.vn", "dangkhoa", "hsg7d25b4")
            self.assertFalse(success)

    def test_sync_dmoj_platform_via_api(self):
        from lms.services import JudgeSyncService
        from django.test import override_settings
        
        with override_settings(VNOJ_API_TOKEN="my_test_token"):
            class MockApiResponse:
                def __init__(self, json_data, status_code):
                    self.json_data = json_data
                    self.status_code = status_code
                def json(self):
                    return self.json_data
            
            with patch('requests.get') as mock_get:
                mock_get.return_value = MockApiResponse({'success': True, 'has_ac': True}, 200)
                
                success = JudgeSyncService._sync_dmoj_platform("http://on.hsgtin.vn", "dangkhoa", "hsg7d25b4")
                self.assertTrue(success)
                mock_get.assert_called_with(
                    "http://on.hsgtin.vn/api/lms-check-submission/",
                    params={'user': 'dangkhoa', 'problem': 'hsg7d25b4'},
                    headers={'Authorization': 'Bearer my_test_token', 'User-Agent': 'Mozilla/5.0'},
                    timeout=10
                )

from django.urls import reverse
from lms.models import Course, CourseBundle, CourseOwnership, WalletTransaction

class CourseBundleTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='bundle_buyer', password='password123')
        self.profile = self.user.profile
        self.profile.wallet_balance = 50000.00
        self.profile.save()
        
        self.course1 = Course.objects.create(title="Course 1", description="Desc 1", price=30000.00)
        self.course2 = Course.objects.create(title="Course 2", description="Desc 2", price=30000.00)
        
        self.bundle = CourseBundle.objects.create(
            title="Super Saver Bundle",
            description="Buy both for less!",
            price=45000.00
        )
        self.bundle.courses.add(self.course1, self.course2)
        
    def test_course_bundle_list_and_detail(self):
        self.client.login(username='bundle_buyer', password='password123')
        
        # Test List View
        response = self.client.get(reverse('course_bundle_list'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'lms/course_bundle_list.html')
        self.assertContains(response, "Super Saver Bundle")
        
        # Test Detail View
        response = self.client.get(reverse('course_bundle_detail', args=[self.bundle.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'lms/course_bundle_detail.html')
        self.assertContains(response, "Course 1")
        self.assertContains(response, "Course 2")
        
    def test_buy_course_bundle_success(self):
        self.client.login(username='bundle_buyer', password='password123')
        
        # Purchase Bundle
        response = self.client.post(reverse('buy_course_bundle', args=[self.bundle.id]))
        self.assertEqual(response.status_code, 302) # Redirects to bundle detail on success
        
        # Verify Wallet Deduction
        self.profile.refresh_from_db()
        self.assertEqual(float(self.profile.wallet_balance), 5000.00)
        
        # Verify Ownership Granted
        self.assertTrue(CourseOwnership.objects.filter(user=self.user, course=self.course1).exists())
        self.assertTrue(CourseOwnership.objects.filter(user=self.user, course=self.course2).exists())
        
        # Verify Transaction Logged
        self.assertTrue(WalletTransaction.objects.filter(user=self.user, amount=45000.00, transaction_type='PAYMENT').exists())

    def test_buy_course_bundle_insufficient_funds(self):
        # Set low wallet balance
        self.profile.wallet_balance = 10000.00
        self.profile.save()
        
        self.client.login(username='bundle_buyer', password='password123')
        
        response = self.client.post(reverse('buy_course_bundle', args=[self.bundle.id]))
        self.assertEqual(response.status_code, 302) # Redirects to deposit or same page
        
        # Balance should be unchanged
        self.profile.refresh_from_db()
        self.assertEqual(float(self.profile.wallet_balance), 10000.00)
        
        # No ownership should be granted
        self.assertFalse(CourseOwnership.objects.filter(user=self.user, course=self.course1).exists())
