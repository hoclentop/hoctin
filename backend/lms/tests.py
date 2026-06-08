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

    def test_bbcode_hide_and_xoa(self):
        from lms.bbcode_parser import BBBienParser
        parser = BBBienParser()
        
        # Test basic hide and xoa
        text = "Hello [hide]World[/hide] and [xoa]Secret[/xoa]!"
        parsed = parser.parse(text)
        self.assertEqual(parsed, "Hello <span style=\"display:none;\">World</span> and !")
        
        # Test hide and xoa with nested content or multiple blocks
        text_multiple = "[hide]Block 1[/hide] some text [xoa]Block 2[/xoa] other text [hide]Block 3[/hide]"
        parsed_multiple = parser.parse(text_multiple)
        self.assertEqual(parsed_multiple, "<span style=\"display:none;\">Block 1</span> some text  other text <span style=\"display:none;\">Block 3</span>")

        # Test basic underline tag [u]...[/u] (retained intact for frontend markdown engine)
        text_u = "This is [u]underlined text[/u]."
        parsed_u = parser.parse(text_u)
        self.assertEqual(parsed_u, "This is [u]underlined text[/u].")

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

    def test_sequential_test_requires_100_percent(self):
        from django.urls import reverse
        from lms.models import Test, LessonProgress, TestQuestion, Question, Choice
        
        # Create a test with a question worth 10 points
        test_obj = Test.objects.create(
            title='Sequential Hard Test',
            price=0.0,
            duration=30,
            creator=self.admin
        )
        
        q = Question.objects.create(
            content='Hard Question',
            question_type=1, # Single Choice
        )
        c_correct = Choice.objects.create(question=q, content='Correct', is_correct=True)
        c_incorrect = Choice.objects.create(question=q, content='Incorrect', is_correct=False)
        
        tq = TestQuestion.objects.create(
            test=test_obj,
            question=q,
            points=10.0
        )
        
        # Create a lesson linked to this test inside the sequential course
        test_lesson = Lesson.objects.create(
            title='Sequential Test Lesson',
            course=self.free_course,
            lesson_type='TEST',
            test=test_obj,
            order_index=4
        )
        
        CourseOwnership.objects.get_or_create(user=self.student, course=self.free_course)
        
        # Scenario A: Student attempts but gets 0/10 points (incorrect choice)
        attempt1 = Attempt.objects.create(
            user=self.student,
            test=test_obj,
            start_time=timezone.now()
        )
        
        self.client.login(username='student1', password='password123')
        # Submit incorrect choice
        response = self.client.post(reverse('submit_test', args=[attempt1.id]), {
            f'q_{tq.id}': [c_incorrect.id]
        })
        self.assertEqual(response.status_code, 302)
        
        # Verify the lesson progress is NOT completed (since it's sequential and score is 0/10)
        is_completed = LessonProgress.objects.filter(user=self.student, lesson=test_lesson, is_completed=True).exists()
        self.assertFalse(is_completed)
        
        # Scenario B: Student re-attempts and gets 10/10 points (correct choice)
        attempt2 = Attempt.objects.create(
            user=self.student,
            test=test_obj,
            start_time=timezone.now()
        )
        
        # Submit correct choice
        response = self.client.post(reverse('submit_test', args=[attempt2.id]), {
            f'q_{tq.id}': [c_correct.id]
        })
        self.assertEqual(response.status_code, 302)
        
        # Verify the lesson progress IS completed now
        is_completed = LessonProgress.objects.filter(user=self.student, lesson=test_lesson, is_completed=True).exists()
        self.assertTrue(is_completed)

    def test_trial_user_lesson_completion_exclusion(self):
        from django.urls import reverse
        from lms.models import LessonProgress
        
        # Scenario A: Course Creator tries to complete a lesson
        self.client.login(username='thnam', password='password123')
        
        response = self.client.post(reverse('complete_lesson_ajax', args=[self.lesson1.id]))
        self.assertEqual(response.status_code, 200)
        
        # Verify the lesson progress is NOT completed (no record created)
        is_completed = LessonProgress.objects.filter(user=self.teacher, lesson=self.lesson1).exists()
        self.assertFalse(is_completed)
        
        # Scenario B: Admin tries to complete a lesson
        self.client.login(username='admin', password='adminpassword')
        
        response = self.client.post(reverse('complete_lesson_ajax', args=[self.lesson1.id]))
        self.assertEqual(response.status_code, 200)
        
        is_completed = LessonProgress.objects.filter(user=self.admin, lesson=self.lesson1).exists()
        self.assertFalse(is_completed)

    def test_trial_user_activities_and_leaderboard_exclusion(self):
        from django.urls import reverse
        from lms.models import Test, Attempt, LessonProgress
        
        # Create a test
        test_obj = Test.objects.create(
            title='Trial Test Activity',
            price=0.0,
            duration=30,
            creator=self.teacher
        )
        
        # Student attempt
        Attempt.objects.create(
            user=self.student,
            test=test_obj,
            start_time=timezone.now(),
            end_time=timezone.now(),
            total_score=10.0,
            is_official=True
        )
        
        # Teacher (creator) attempt
        Attempt.objects.create(
            user=self.teacher,
            test=test_obj,
            start_time=timezone.now(),
            end_time=timezone.now(),
            total_score=10.0,
            is_official=True
        )
        
        # Admin attempt
        Attempt.objects.create(
            user=self.admin,
            test=test_obj,
            start_time=timezone.now(),
            end_time=timezone.now(),
            total_score=10.0,
            is_official=True
        )
        
        # Create lesson completion for student
        LessonProgress.objects.create(
            user=self.student,
            lesson=self.lesson1,
            is_completed=True,
            completed_at=timezone.now()
        )
        
        # Fetch course list homepage
        response = self.client.get(reverse('course_list'))
        self.assertEqual(response.status_code, 200)
        
        activities = response.context['activities']
        # The timeline should contain the student's activities but NOT teacher's or admin's
        active_usernames = [act['user'].username for act in activities]
        self.assertIn('student1', active_usernames)
        self.assertNotIn('thnam', active_usernames)
        self.assertNotIn('admin', active_usernames)
        
        # Fetch leaderboard
        self.client.login(username='student1', password='password123')
        response = self.client.get(reverse('leaderboard', args=[test_obj.id]))
        self.assertEqual(response.status_code, 200)
        
        leaderboard_list = response.context['leaderboard']
        leaderboard_usernames = [attempt.user.username for attempt in leaderboard_list]
        self.assertIn('student1', leaderboard_usernames)
        self.assertNotIn('thnam', leaderboard_usernames)
        self.assertNotIn('admin', leaderboard_usernames)

    def test_multi_exercise_url_parsing_and_helpers(self):
        from lms.views import parse_external_link_helper, extract_exercise_info_helper
        
        # Test parse_external_link_helper
        plat, code = parse_external_link_helper("https://oj.vnoi.info/problem/qmax")
        self.assertEqual(plat, "vnoj")
        self.assertEqual(code, "qmax")
        
        plat, code = parse_external_link_helper("http://on.hsgtin.vn/problem/hsg7d25b4")
        self.assertEqual(plat, "hsgtin")
        self.assertEqual(code, "hsg7d25b4")
        
        plat, code = parse_external_link_helper("https://codeforces.com/contest/1800/problem/C")
        self.assertEqual(plat, "codeforces")
        self.assertEqual(code, "1800C")
        
        plat, code = parse_external_link_helper("https://codeforces.com/problemset/problem/1800/C")
        self.assertEqual(plat, "codeforces")
        self.assertEqual(code, "1800C")
        
        # Test extract_exercise_info_helper
        url, plat, code, is_hard = extract_exercise_info_helper("https://oj.vnoi.info/problem/qmax *")
        self.assertEqual(url, "https://oj.vnoi.info/problem/qmax")
        self.assertEqual(plat, "vnoj")
        self.assertEqual(code, "qmax")
        self.assertTrue(is_hard)
        
        url, plat, code, is_hard = extract_exercise_info_helper("- [QMAX](https://oj.vnoi.info/problem/qmax)")
        self.assertEqual(url, "https://oj.vnoi.info/problem/qmax")
        self.assertEqual(plat, "vnoj")
        self.assertEqual(code, "qmax")
        self.assertFalse(is_hard)

    def test_multi_exercise_lesson_flow_and_sync(self):
        from lms.models import MultiExerciseProgress, LessonProgress
        from django.urls import reverse
        from unittest.mock import patch
        
        # Create MULTI_EXERCISE lesson
        multi_lesson = Lesson.objects.create(
            title='Multi Exercise Lesson',
            course=self.paid_course,
            lesson_type='MULTI_EXERCISE',
            content="https://oj.vnoi.info/problem/qmax\nhttps://codeforces.com/contest/1800/problem/C *",
            order_index=5
        )
        
        # Register student in the course
        CourseOwnership.objects.get_or_create(user=self.student, course=self.paid_course)
        self.student.profile.vnoj_username = "student_vnoj"
        self.student.profile.codeforces_username = "student_cf"
        self.student.profile.save()
        
        self.client.login(username='student1', password='password123')
        
        # 1. Access detail view
        response = self.client.get(reverse('lesson_detail', args=[self.paid_course.id, multi_lesson.id]))
        self.assertEqual(response.status_code, 200)
        self.assertIn('multi_exercises', response.context)
        exercises = response.context['multi_exercises']
        self.assertEqual(len(exercises), 2)
        
        # 2. Sync the required exercise (vnoj qmax) - Mock sync success
        with patch('lms.services.JudgeSyncService.sync_vnoj', return_value=True):
            response = self.client.post(
                reverse('sync_multi_exercise_ajax', args=[multi_lesson.id]),
                {"link": "https://oj.vnoi.info/problem/qmax"},
                content_type="application/json"
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data['success'])
            
            # Since only vnoj qmax is required (cf is hard *), lesson should be completed now!
            self.assertTrue(data['lesson_completed'])
            
            # Verify database records
            self.assertTrue(MultiExerciseProgress.objects.filter(user=self.student, lesson=multi_lesson, link="https://oj.vnoi.info/problem/qmax", is_completed=True).exists())
            self.assertTrue(LessonProgress.objects.filter(user=self.student, lesson=multi_lesson, is_completed=True).exists())

class ProfileTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='password123')
        
    def test_register_user_success(self):
        from django.urls import reverse
        from django.contrib.auth.models import User
        
        post_data = {
            'username': 'new_registered_user',
            'password1': 'securepass123',
            'password2': 'securepass123',
        }
        response = self.client.post(reverse('register'), post_data)
        self.assertEqual(response.status_code, 302) # redirects to course_list on success
        
        # Verify user and profile created
        new_user = User.objects.get(username='new_registered_user')
        self.assertIsNotNone(new_user)
        self.assertIsNotNone(new_user.profile)
        
    def test_password_change_success(self):
        from django.urls import reverse
        self.client.login(username='tester', password='password123')
        
        # Get password change page
        response = self.client.get(reverse('password_change'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'registration/password_change.html')
        
        # Post change password form
        post_data = {
            'old_password': 'password123',
            'new_password1': 'newsecurepassword123',
            'new_password2': 'newsecurepassword123',
        }
        response = self.client.post(reverse('password_change'), post_data)
        self.assertEqual(response.status_code, 302) # Redirect to password change done page
        
        # Verify done page redirects
        response = self.client.get(reverse('password_change_done'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'registration/password_change_done.html')
        
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


class AdminWalletTransactionTestCase(TestCase):
    def setUp(self):
        # Create users
        self.student = User.objects.create_user(username='student_tx', password='password123')
        self.student_profile = self.student.profile
        self.student_profile.wallet_balance = 0.00
        self.student_profile.save()
        
        self.superuser = User.objects.create_superuser(username='admin_tx', password='adminpassword')
        
        # Create a pending transaction
        self.tx = WalletTransaction.objects.create(
            user=self.student,
            amount=100000.00,
            transaction_type='DEPOSIT',
            status='PENDING'
        )

    def test_admin_transactions_view_requires_superuser(self):
        # Anonymous user redirected to login
        response = self.client.get(reverse('admin_transactions'))
        self.assertEqual(response.status_code, 302)
        
        # Student user redirected to course list with error message
        self.client.login(username='student_tx', password='password123')
        response = self.client.get(reverse('admin_transactions'))
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('course_list'))
        
        # Superuser successfully accesses page
        self.client.login(username='admin_tx', password='adminpassword')
        response = self.client.get(reverse('admin_transactions'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'lms/admin_transactions.html')
        self.assertContains(response, "student_tx")
        self.assertContains(response, "+100000đ")

    def test_approve_transaction_admin_success(self):
        # Verify student balance is 0
        self.assertEqual(float(self.student_profile.wallet_balance), 0.00)
        
        # Superuser approves transaction
        self.client.login(username='admin_tx', password='adminpassword')
        response = self.client.post(reverse('approve_transaction_admin', args=[self.tx.id]))
        self.assertRedirects(response, reverse('admin_transactions'))
        
        # Verify transaction status and user balance
        self.tx.refresh_from_db()
        self.assertEqual(self.tx.status, 'APPROVED')
        
        self.student_profile.refresh_from_db()
        self.assertEqual(float(self.student_profile.wallet_balance), 100000.00)

    def test_approve_transaction_admin_requires_superuser(self):
        # Student tries to approve
        self.client.login(username='student_tx', password='password123')
        response = self.client.post(reverse('approve_transaction_admin', args=[self.tx.id]))
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('course_list'))
        
        # Verify status and balance unchanged
        self.tx.refresh_from_db()
        self.assertEqual(self.tx.status, 'PENDING')
        self.student_profile.refresh_from_db()
        self.assertEqual(float(self.student_profile.wallet_balance), 0.00)

    def test_approve_transaction_admin_non_post_rejected(self):
        # GET request should redirect and do nothing
        self.client.login(username='admin_tx', password='adminpassword')
        response = self.client.get(reverse('approve_transaction_admin', args=[self.tx.id]))
        self.assertRedirects(response, reverse('admin_transactions'))
        
        # Verify transaction status and user balance unchanged
        self.tx.refresh_from_db()
        self.assertEqual(self.tx.status, 'PENDING')
        self.student_profile.refresh_from_db()
        self.assertEqual(float(self.student_profile.wallet_balance), 0.00)

    def test_reject_transaction_admin_success(self):
        # Verify student balance is 0
        self.assertEqual(float(self.student_profile.wallet_balance), 0.00)
        
        # Superuser rejects transaction
        self.client.login(username='admin_tx', password='adminpassword')
        response = self.client.post(reverse('reject_transaction_admin', args=[self.tx.id]))
        self.assertRedirects(response, reverse('admin_transactions'))
        
        # Verify transaction status and user balance
        self.tx.refresh_from_db()
        self.assertEqual(self.tx.status, 'REJECTED')
        
        self.student_profile.refresh_from_db()
        self.assertEqual(float(self.student_profile.wallet_balance), 0.00)

    def test_reject_transaction_admin_requires_superuser(self):
        # Student tries to reject
        self.client.login(username='student_tx', password='password123')
        response = self.client.post(reverse('reject_transaction_admin', args=[self.tx.id]))
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('course_list'))
        
        # Verify status and balance unchanged
        self.tx.refresh_from_db()
        self.assertEqual(self.tx.status, 'PENDING')
        self.student_profile.refresh_from_db()
        self.assertEqual(float(self.student_profile.wallet_balance), 0.00)

    def test_reject_transaction_admin_non_post_rejected(self):
        # GET request should redirect and do nothing
        self.client.login(username='admin_tx', password='adminpassword')
        response = self.client.get(reverse('reject_transaction_admin', args=[self.tx.id]))
        self.assertRedirects(response, reverse('admin_transactions'))
        
        # Verify transaction status and user balance unchanged
        self.tx.refresh_from_db()
        self.assertEqual(self.tx.status, 'PENDING')
        self.student_profile.refresh_from_db()
        self.assertEqual(float(self.student_profile.wallet_balance), 0.00)


class OfflinePaymentTestCase(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from django.core.files.uploadedfile import SimpleUploadedFile
        from lms.models import Course, CourseBundle, Test, TestBundle, BankAccount
        
        # Create users
        self.student = User.objects.create_user(username='student_offline', password='password123')
        self.student_profile = self.student.profile
        self.student_profile.wallet_balance = 0.00
        self.student_profile.save()
        
        self.admin = User.objects.create_superuser(username='admin_offline', password='adminpassword')
        
        # Create bank accounts
        self.bank_active_mb = BankAccount.objects.create(
            bank_name='MB Bank',
            bank_code='MB',
            account_number='0987654321',
            account_holder='HOCTIN LMS',
            is_active=True
        )
        self.bank_active_vcb = BankAccount.objects.create(
            bank_name='Vietcombank',
            bank_code='VCB',
            account_number='1234567890',
            account_holder='HOCTIN LMS',
            is_active=True
        )
        self.bank_inactive = BankAccount.objects.create(
            bank_name='DongA Bank',
            bank_code='DAB',
            account_number='9999999999',
            account_holder='HOCTIN LMS',
            is_active=False
        )

        # Create a course
        self.course = Course.objects.create(
            title='Offline Python Course',
            description='Learn Python Offline',
            price=150000.00
        )
        
        # Create a CourseBundle
        self.course_in_bundle1 = Course.objects.create(title='Combo Course 1', price=50000.00)
        self.course_in_bundle2 = Course.objects.create(title='Combo Course 2', price=50000.00)
        self.course_bundle = CourseBundle.objects.create(
            title='Offline Combo Course',
            description='Learn Combo Offline',
            price=80000.00
        )
        self.course_bundle.courses.add(self.course_in_bundle1, self.course_in_bundle2)
        
        # Create a Test
        self.test = Test.objects.create(
            title='Offline Practice Exam',
            price=20000.00,
            duration=45
        )
        
        # Create a TestBundle
        self.test_in_bundle1 = Test.objects.create(title='Combo Test 1', price=10000.00)
        self.test_in_bundle2 = Test.objects.create(title='Combo Test 2', price=10000.00)
        self.test_bundle = TestBundle.objects.create(
            title='Offline Combo Test',
            description='Learn Test Combo Offline',
            price=15000.00
        )
        self.test_bundle.tests.add(self.test_in_bundle1, self.test_in_bundle2)
        
        # Mock file
        self.mock_image = SimpleUploadedFile("proof.png", b"file_content", content_type="image/png")

    def test_course_offline_checkout_and_approval(self):
        from django.urls import reverse
        from lms.models import WalletTransaction, CourseOwnership
        
        self.client.login(username='student_offline', password='password123')
        
        # 1. POST to course_checkout with offline payment
        response = self.client.post(reverse('course_checkout', args=[self.course.id]), {
            'payment_method': 'offline',
            'proof_image': self.mock_image
        })
        self.assertEqual(response.status_code, 302)
        
        # 2. Verify transaction created with status PENDING and correct details
        tx = WalletTransaction.objects.filter(user=self.student, course=self.course).first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.status, 'PENDING')
        self.assertEqual(tx.transaction_type, 'PAYMENT')
        self.assertEqual(float(tx.amount), 150000.00)
        
        # 3. Verify has_pending_transaction is True in detail view
        response = self.client.get(reverse('course_detail', args=[self.course.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['has_pending_transaction'])
        
        # 4. Admin approves
        self.client.login(username='admin_offline', password='adminpassword')
        response = self.client.post(reverse('approve_transaction_admin', args=[tx.id]))
        self.assertEqual(response.status_code, 302)
        
        # 5. Verify status is APPROVED and ownership is granted
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'APPROVED')
        self.assertTrue(CourseOwnership.objects.filter(user=self.student, course=self.course).exists())
        
        # 6. Verify wallet balance remains unchanged
        self.student_profile.refresh_from_db()
        self.assertEqual(float(self.student_profile.wallet_balance), 0.00)

    def test_course_bundle_offline_checkout_and_rejection(self):
        from django.urls import reverse
        from lms.models import WalletTransaction, CourseOwnership
        
        self.client.login(username='student_offline', password='password123')
        
        # 1. POST to course_bundle_checkout with offline payment
        response = self.client.post(reverse('course_bundle_checkout', args=[self.course_bundle.id]), {
            'payment_method': 'offline',
            'proof_image': self.mock_image
        })
        self.assertEqual(response.status_code, 302)
        
        # 2. Verify transaction created with status PENDING
        tx = WalletTransaction.objects.filter(user=self.student, course_bundle=self.course_bundle).first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.status, 'PENDING')
        
        # 3. Verify has_pending_transaction is True in bundle detail view
        response = self.client.get(reverse('course_bundle_detail', args=[self.course_bundle.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['has_pending_transaction'])
        
        # 4. Admin rejects
        self.client.login(username='admin_offline', password='adminpassword')
        response = self.client.post(reverse('reject_transaction_admin', args=[tx.id]))
        self.assertEqual(response.status_code, 302)
        
        # 5. Verify status is REJECTED and no ownership is granted
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'REJECTED')
        self.assertFalse(CourseOwnership.objects.filter(user=self.student, course=self.course_in_bundle1).exists())
        self.assertFalse(CourseOwnership.objects.filter(user=self.student, course=self.course_in_bundle2).exists())
        
        # 6. Verify wallet balance remains unchanged
        self.student_profile.refresh_from_db()
        self.assertEqual(float(self.student_profile.wallet_balance), 0.00)

    def test_test_offline_checkout_rules_agreement(self):
        from django.urls import reverse
        from lms.models import WalletTransaction, TestOwnership
        
        self.client.login(username='student_offline', password='password123')
        
        # 1. POST to test_checkout with offline payment but without rules agreement
        response = self.client.post(reverse('test_checkout', args=[self.test.id]), {
            'payment_method': 'offline',
            'proof_image': self.mock_image
        })
        self.assertEqual(response.status_code, 200) # Re-renders checkout due to error
        self.assertFalse(WalletTransaction.objects.filter(user=self.student, test=self.test).exists())
        
        # 2. POST to test_checkout with offline payment AND rules agreement
        response = self.client.post(reverse('test_checkout', args=[self.test.id]), {
            'payment_method': 'offline',
            'proof_image': self.mock_image,
            'agree_rules': 'on'
        })
        self.assertEqual(response.status_code, 302)
        
        # 3. Verify transaction created with status PENDING
        tx = WalletTransaction.objects.filter(user=self.student, test=self.test).first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.status, 'PENDING')
        
        # 4. Verify has_pending_transaction in test detail view
        response = self.client.get(reverse('test_detail', args=[self.test.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['has_pending_transaction'])
        
        # 5. Admin approves
        self.client.login(username='admin_offline', password='adminpassword')
        response = self.client.post(reverse('approve_transaction_admin', args=[tx.id]))
        self.assertEqual(response.status_code, 302)
        
        # 6. Verify status is APPROVED and TestOwnership is granted with agreement
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'APPROVED')
        
        ownership = TestOwnership.objects.filter(user=self.student, test=self.test).first()
        self.assertIsNotNone(ownership)
        self.assertTrue(ownership.agreed_rules)
        self.assertIsNotNone(ownership.registered_at)

    def test_test_bundle_offline_checkout_and_approval(self):
        from django.urls import reverse
        from lms.models import WalletTransaction, TestOwnership
        
        self.client.login(username='student_offline', password='password123')
        
        # 1. POST to test_bundle_checkout with offline payment
        response = self.client.post(reverse('test_bundle_checkout', args=[self.test_bundle.id]), {
            'payment_method': 'offline',
            'proof_image': self.mock_image
        })
        self.assertEqual(response.status_code, 302)
        
        # 2. Verify transaction created with status PENDING
        tx = WalletTransaction.objects.filter(user=self.student, test_bundle=self.test_bundle).first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.status, 'PENDING')
        
        # 3. Verify has_pending_transaction in test bundle detail view
        response = self.client.get(reverse('test_bundle_detail', args=[self.test_bundle.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['has_pending_transaction'])
        
        # 4. Admin approves
        self.client.login(username='admin_offline', password='adminpassword')
        response = self.client.post(reverse('approve_transaction_admin', args=[tx.id]))
        self.assertEqual(response.status_code, 302)
        
        # 5. Verify status is APPROVED and TestOwnership for all tests in the bundle is granted with agreement
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'APPROVED')
        
        ownership1 = TestOwnership.objects.filter(user=self.student, test=self.test_in_bundle1).first()
        self.assertIsNotNone(ownership1)
        self.assertTrue(ownership1.agreed_rules)
        self.assertIsNotNone(ownership1.registered_at)
        
        ownership2 = TestOwnership.objects.filter(user=self.student, test=self.test_in_bundle2).first()
        self.assertIsNotNone(ownership2)
        self.assertTrue(ownership2.agreed_rules)
        self.assertIsNotNone(ownership2.registered_at)

    def test_multi_bank_account_checkout(self):
        from django.urls import reverse
        from lms.models import WalletTransaction, BankAccount
        
        self.client.login(username='student_offline', password='password123')
        
        # 1. GET to checkout and verify active bank accounts are present in context, and inactive is NOT.
        response = self.client.get(reverse('course_checkout', args=[self.course.id]))
        self.assertEqual(response.status_code, 200)
        
        bank_accounts = list(response.context['bank_accounts'])
        self.assertIn(self.bank_active_mb, bank_accounts)
        self.assertIn(self.bank_active_vcb, bank_accounts)
        self.assertNotIn(self.bank_inactive, bank_accounts)
        
        # 2. POST offline checkout with valid active bank account ID (MB Bank)
        response = self.client.post(reverse('course_checkout', args=[self.course.id]), {
            'payment_method': 'offline',
            'proof_image': self.mock_image,
            'bank_account_id': self.bank_active_mb.id
        })
        self.assertEqual(response.status_code, 302)
        
        tx_mb = WalletTransaction.objects.filter(user=self.student, course=self.course, status='PENDING').first()
        self.assertIsNotNone(tx_mb)
        self.assertEqual(tx_mb.bank_account, self.bank_active_mb)
        
        # Clean up the transaction so we can checkout again
        tx_mb.delete()
        
        # 3. POST offline checkout with valid active bank account ID (Vietcombank)
        response = self.client.post(reverse('course_checkout', args=[self.course.id]), {
            'payment_method': 'offline',
            'proof_image': self.mock_image,
            'bank_account_id': self.bank_active_vcb.id
        })
        self.assertEqual(response.status_code, 302)
        
        tx_vcb = WalletTransaction.objects.filter(user=self.student, course=self.course, status='PENDING').first()
        self.assertIsNotNone(tx_vcb)
        self.assertEqual(tx_vcb.bank_account, self.bank_active_vcb)
        
        # Clean up
        tx_vcb.delete()
        
        # 4. POST offline checkout with inactive bank account ID
        response = self.client.post(reverse('course_checkout', args=[self.course.id]), {
            'payment_method': 'offline',
            'proof_image': self.mock_image,
            'bank_account_id': self.bank_inactive.id
        })
        self.assertEqual(response.status_code, 302)
        
        tx_inactive = WalletTransaction.objects.filter(user=self.student, course=self.course, status='PENDING').first()
        self.assertIsNotNone(tx_inactive)
        self.assertIsNone(tx_inactive.bank_account)


class WalletTransactionAdminApprovalTestCase(TestCase):
    def setUp(self):
        from django.contrib.admin.sites import AdminSite
        from lms.admin import WalletTransactionAdmin
        from lms.models import WalletTransaction, Course, CourseOwnership, Profile
        from django.contrib.auth.models import User
        
        self.site = AdminSite()
        self.admin = WalletTransactionAdmin(WalletTransaction, self.site)
        
        self.student = User.objects.create_user(username='student_admin_tx', password='password123')
        self.course = Course.objects.create(title="Django Admin Course", price=120000.00)
        
    def test_deposit_approval_via_bulk_action(self):
        from lms.models import WalletTransaction
        tx = WalletTransaction.objects.create(
            user=self.student,
            amount=50000.00,
            transaction_type='DEPOSIT',
            status='PENDING'
        )
        queryset = WalletTransaction.objects.filter(id=tx.id)
        self.admin.approve_transaction(request=None, queryset=queryset)
        
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'APPROVED')
        profile = self.student.profile
        profile.refresh_from_db()
        self.assertEqual(float(profile.wallet_balance), 50000.00)
        
    def test_payment_approval_via_bulk_action(self):
        from lms.models import WalletTransaction, CourseOwnership
        tx = WalletTransaction.objects.create(
            user=self.student,
            amount=120000.00,
            transaction_type='PAYMENT',
            course=self.course,
            status='PENDING'
        )
        queryset = WalletTransaction.objects.filter(id=tx.id)
        self.admin.approve_transaction(request=None, queryset=queryset)
        
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'APPROVED')
        self.assertTrue(CourseOwnership.objects.filter(user=self.student, course=self.course).exists())
        profile = self.student.profile
        profile.refresh_from_db()
        self.assertEqual(float(profile.wallet_balance), 0.00)
        
    def test_payment_approval_via_save_model(self):
        from lms.models import WalletTransaction, CourseOwnership
        tx = WalletTransaction.objects.create(
            user=self.student,
            amount=120000.00,
            transaction_type='PAYMENT',
            course=self.course,
            status='PENDING'
        )
        
        # Simulate admin changing status to APPROVED in the change form and saving
        tx.status = 'APPROVED'
        self.admin.save_model(request=None, obj=tx, form=None, change=True)
        
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'APPROVED')
        self.assertTrue(CourseOwnership.objects.filter(user=self.student, course=self.course).exists())
        profile = self.student.profile
        profile.refresh_from_db()
        self.assertEqual(float(profile.wallet_balance), 0.00)

    def test_course_bundle_approval_via_save_model(self):
        from lms.models import WalletTransaction, CourseOwnership, CourseBundle
        course2 = Course.objects.create(title="Django Admin Course 2", price=80000.00)
        bundle = CourseBundle.objects.create(title="Combo Admin", price=150000.00)
        bundle.courses.add(self.course, course2)
        
        tx = WalletTransaction.objects.create(
            user=self.student,
            amount=150000.00,
            transaction_type='PAYMENT',
            course_bundle=bundle,
            status='PENDING'
        )
        
        tx.status = 'APPROVED'
        self.admin.save_model(request=None, obj=tx, form=None, change=True)
        
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'APPROVED')
        self.assertTrue(CourseOwnership.objects.filter(user=self.student, course=self.course).exists())
        self.assertTrue(CourseOwnership.objects.filter(user=self.student, course=course2).exists())

    def test_test_and_test_bundle_approval_via_save_model(self):
        from lms.models import WalletTransaction, TestOwnership, Test, TestBundle
        test1 = Test.objects.create(title="Admin Test 1", price=5000.00)
        test2 = Test.objects.create(title="Admin Test 2", price=10000.00)
        test_bundle = TestBundle.objects.create(title="Admin Test Bundle", price=12000.00)
        test_bundle.tests.add(test1, test2)
        
        tx_test = WalletTransaction.objects.create(
            user=self.student,
            amount=5000.00,
            transaction_type='PAYMENT',
            test=test1,
            status='PENDING'
        )
        tx_test.status = 'APPROVED'
        self.admin.save_model(request=None, obj=tx_test, form=None, change=True)
        
        tx_test.refresh_from_db()
        self.assertEqual(tx_test.status, 'APPROVED')
        ownership = TestOwnership.objects.filter(user=self.student, test=test1).first()
        self.assertIsNotNone(ownership)
        self.assertTrue(ownership.agreed_rules)
        self.assertIsNotNone(ownership.registered_at)
        
        tx_bundle = WalletTransaction.objects.create(
            user=self.student,
            amount=12000.00,
            transaction_type='PAYMENT',
            test_bundle=test_bundle,
            status='PENDING'
        )
        tx_bundle.status = 'APPROVED'
        self.admin.save_model(request=None, obj=tx_bundle, form=None, change=True)
        
        tx_bundle.refresh_from_db()
        self.assertEqual(tx_bundle.status, 'APPROVED')
        self.assertTrue(TestOwnership.objects.filter(user=self.student, test=test1).exists())
        self.assertTrue(TestOwnership.objects.filter(user=self.student, test=test2).exists())


class CourseExpiryAndExtensionTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(username="student", password="password")
        # Đảm bảo Profile được tạo và có ví tiền đầy đủ
        self.student.profile.wallet_balance = 200000.00
        self.student.profile.save()
        
        self.course_30_days = Course.objects.create(
            title="Khóa học 30 ngày",
            price=100000.00,
            duration_days=30
        )
        
        self.course_lifetime = Course.objects.create(
            title="Khóa học trọn đời",
            price=50000.00,
            duration_days=0
        )
        
    def test_buy_course_calculates_expiry(self):
        from django.utils import timezone
        import datetime
        
        # Mua khóa học 30 ngày
        self.client.login(username="student", password="password")
        response = self.client.post(reverse('buy_course', args=[self.course_30_days.id]))
        self.assertEqual(response.status_code, 302)
        
        # Kiểm tra expires_at
        ownership = CourseOwnership.objects.get(user=self.student, course=self.course_30_days)
        self.assertIsNotNone(ownership.expires_at)
        
        # Đảm bảo expires_at xấp xỉ 30 ngày sau
        now = timezone.now()
        expected_expiry = now + datetime.timedelta(days=30)
        self.assertAlmostEqual(ownership.expires_at, expected_expiry, delta=datetime.timedelta(seconds=5))
        self.assertFalse(ownership.is_expired)
        
    def test_lifetime_course_no_expiry(self):
        self.client.login(username="student", password="password")
        response = self.client.post(reverse('buy_course', args=[self.course_lifetime.id]))
        self.assertEqual(response.status_code, 302)
        
        ownership = CourseOwnership.objects.get(user=self.student, course=self.course_lifetime)
        self.assertIsNone(ownership.expires_at)
        self.assertFalse(ownership.is_expired)
        
    def test_expired_course_blocks_lesson_access(self):
        from django.utils import timezone
        import datetime
        
        lesson = Lesson.objects.create(
            course=self.course_30_days,
            title="Bài học 1",
            lesson_type="THEORY",
            order_index=1
        )
        
        # Tạo ownership hết hạn sẵn
        ownership = CourseOwnership.objects.create(
            user=self.student,
            course=self.course_30_days,
            expires_at=timezone.now() - datetime.timedelta(days=1)
        )
        self.assertTrue(ownership.is_expired)
        self.assertFalse(CourseOwnership.has_active_ownership(self.student, self.course_30_days))
        
        # Thử truy cập lesson
        self.client.login(username="student", password="password")
        response = self.client.get(reverse('lesson_detail', args=[self.course_30_days.id, lesson.id]))
        # Phải redirect về course_detail
        self.assertEqual(response.status_code, 302)
        
    def test_admin_extension_restores_access(self):
        from django.utils import timezone
        import datetime
        
        lesson = Lesson.objects.create(
            course=self.course_30_days,
            title="Bài học 1",
            lesson_type="THEORY",
            order_index=1
        )
        
        # Tạo ownership hết hạn
        ownership = CourseOwnership.objects.create(
            user=self.student,
            course=self.course_30_days,
            expires_at=timezone.now() - datetime.timedelta(days=1)
        )
        self.assertTrue(ownership.is_expired)
        
        # Thử truy cập lesson -> bị block (redirect)
        self.client.login(username="student", password="password")
        response = self.client.get(reverse('lesson_detail', args=[self.course_30_days.id, lesson.id]))
        self.assertEqual(response.status_code, 302)
        
        # Admin gia hạn (cộng thêm 10 ngày trong tương lai)
        ownership.expires_at = timezone.now() + datetime.timedelta(days=10)
        ownership.save()
        
        # Thử truy cập lesson -> thành công (200 OK)
        response = self.client.get(reverse('lesson_detail', args=[self.course_30_days.id, lesson.id]))
        self.assertEqual(response.status_code, 200)

    def test_repurchase_after_expiration(self):
        from django.utils import timezone
        import datetime
        
        # Tạo ownership hết hạn
        ownership = CourseOwnership.objects.create(
            user=self.student,
            course=self.course_30_days,
            expires_at=timezone.now() - datetime.timedelta(days=5)
        )
        self.assertTrue(ownership.is_expired)
        
        # Mua lại
        self.client.login(username="student", password="password")
        response = self.client.post(reverse('buy_course', args=[self.course_30_days.id]))
        self.assertEqual(response.status_code, 302)
        
        # Kiểm tra expires_at mới
        ownership.refresh_from_db()
        self.assertFalse(ownership.is_expired)
        expected_expiry = timezone.now() + datetime.timedelta(days=30)
        self.assertAlmostEqual(ownership.expires_at, expected_expiry, delta=datetime.timedelta(seconds=5))


class TeacherDashboardTestCase(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from lms.models import Course, Lesson, LessonProgress, CourseOwnership
        
        # Tạo 2 giáo viên
        self.teacher1 = User.objects.create_user(username="teacher1", password="password")
        self.teacher1.profile.can_create_courses = True
        self.teacher1.profile.save()
        
        self.teacher2 = User.objects.create_user(username="teacher2", password="password")
        self.teacher2.profile.can_create_courses = True
        self.teacher2.profile.save()
        
        # Tạo 1 học sinh
        self.student = User.objects.create_user(username="student", password="password")
        
        # Tạo khóa học cho teacher1
        self.course1 = Course.objects.create(
            title="Course 1",
            description="Desc 1",
            creator=self.teacher1
        )
        # Thêm 4 bài học cho course1
        self.lesson1 = Lesson.objects.create(course=self.course1, title="L1", order_index=1, lesson_type="THEORY")
        self.lesson2 = Lesson.objects.create(course=self.course1, title="L2", order_index=2, lesson_type="THEORY")
        self.lesson3 = Lesson.objects.create(course=self.course1, title="L3", order_index=3, lesson_type="EXERCISE")
        self.lesson4 = Lesson.objects.create(course=self.course1, title="L4", order_index=4, lesson_type="TEST")
        
        # Tạo khóa học cho teacher2
        self.course2 = Course.objects.create(
            title="Course 2",
            description="Desc 2",
            creator=self.teacher2
        )
        
        # Học sinh đăng ký course1
        CourseOwnership.objects.create(user=self.student, course=self.course1)
        
    def test_student_cannot_access_dashboard(self):
        from django.urls import reverse
        self.client.login(username="student", password="password")
        
        # Thử vào danh sách khóa học của giáo viên
        response = self.client.get(reverse('teacher_courses_progress'))
        self.assertEqual(response.status_code, 302) # Bị chặn, redirect
        
        # Thử vào chi tiết tiến độ khóa học
        response = self.client.get(reverse('teacher_course_detail_progress', args=[self.course1.id]))
        self.assertEqual(response.status_code, 302)
        
    def test_teacher_access_own_course_dashboard(self):
        from django.urls import reverse
        self.client.login(username="teacher1", password="password")
        
        # Truy cập danh sách khóa học
        response = self.client.get(reverse('teacher_courses_progress'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Course 1")
        self.assertNotContains(response, "Course 2") # Không hiện khóa học của người khác
        
        # Truy cập chi tiết tiến độ khóa học của mình
        response = self.client.get(reverse('teacher_course_detail_progress', args=[self.course1.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "student")
        
    def test_teacher_cannot_access_other_teacher_course(self):
        from django.urls import reverse
        self.client.login(username="teacher1", password="password")
        
        # Thử truy cập chi tiết tiến độ khóa học của teacher2
        response = self.client.get(reverse('teacher_course_detail_progress', args=[self.course2.id]))
        self.assertEqual(response.status_code, 302) # Bị chặn và redirect
        
    def test_progress_calculation_accuracy(self):
        from django.urls import reverse
        from lms.models import LessonProgress
        
        # Cho học sinh hoàn thành 2 trên 4 bài học (50%)
        LessonProgress.objects.create(user=self.student, lesson=self.lesson1, is_completed=True)
        LessonProgress.objects.create(user=self.student, lesson=self.lesson2, is_completed=True)
        # Bài 3 chưa xong, bài 4 chưa xong
        
        self.client.login(username="teacher1", password="password")
        response = self.client.get(reverse('teacher_course_detail_progress', args=[self.course1.id]))
        self.assertEqual(response.status_code, 200)
        
        # Kiểm tra xem có hiển thị 50% và "2 / 4 bài"
        self.assertContains(response, "50%")
        self.assertContains(response, "2")
        self.assertContains(response, "4 bài")


class HomepageTestCase(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from lms.models import Course, Lesson, LessonProgress, Test, Attempt
        from django.utils import timezone
        import datetime
        
        # 1. Tạo users
        self.student = User.objects.create_user(username="student1", password="password")
        self.teacher = User.objects.create_user(username="teacher1", password="password")
        
        # 2. Tạo khóa học
        self.course_cpp = Course.objects.create(
            title="Khóa học C++ Cơ bản",
            description="Học lập trình C++ từ đầu",
            creator=self.teacher,
            price=100000
        )
        self.course_python = Course.objects.create(
            title="Lập trình Python Nâng cao",
            description="Tìm hiểu Python nâng cao",
            creator=self.teacher,
            price=0
        )
        
        # 3. Tạo bài học
        self.lesson1 = Lesson.objects.create(
            course=self.course_cpp,
            title="Bài 1: Giới thiệu C++",
            order_index=1,
            lesson_type="THEORY"
        )
        self.lesson2 = Lesson.objects.create(
            course=self.course_cpp,
            title="Bài 2: Biến và Hằng",
            order_index=2,
            lesson_type="THEORY"
        )
        
        # 4. Tạo đề thi Standalone
        self.test_tracnghiem = Test.objects.create(
            title="Đề thi Trắc nghiệm C++",
            duration=30,
            test_type="STANDALONE",
            price=0
        )
        self.test_python = Test.objects.create(
            title="Đề thi Python OOP",
            duration=45,
            test_type="STANDALONE",
            price=50000
        )
        # Một đề thi KHÔNG phải STANDALONE (ví dụ LESSON_ONLY) để kiểm tra lọc
        self.test_lesson = Test.objects.create(
            title="Kiểm tra bài học C++",
            duration=15,
            test_type="LESSON_ONLY",
            price=0
        )
        
    def test_homepage_search_dual(self):
        from django.urls import reverse
        
        # Gửi request tìm kiếm với từ khóa "C++"
        response = self.client.get(reverse('course_list') + "?q=C%2B%2B")
        self.assertEqual(response.status_code, 200)
        
        # Đảm bảo kết quả tìm kiếm khóa học và đề thi Standalone có chứa C++ được hiển thị
        self.assertContains(response, "Khóa học C++ Cơ bản")
        self.assertContains(response, "Đề thi Trắc nghiệm C++")
        
        # Đảm bảo đề thi không phải Standalone hoặc không liên quan không xuất hiện
        # (Đề thi "Kiểm tra bài học C++" có test_type="LESSON_ONLY" nên không được xuất hiện trong standalone search)
        self.assertNotContains(response, "Kiểm tra bài học C++")
        self.assertNotContains(response, "Lập trình Python Nâng cao")
        
    def test_homepage_activities_aggregation(self):
        from django.urls import reverse
        from lms.models import LessonProgress, Attempt
        from django.utils import timezone
        import datetime
        
        now = timezone.now()
        
        # Tạo 4 hoạt động bài học và 4 hoạt động thi thử với các timestamp khác nhau
        # Hoạt động bài học hoàn thành
        p1 = LessonProgress.objects.create(user=self.student, lesson=self.lesson1, is_completed=True)
        p1.completed_at = now - datetime.timedelta(minutes=10)
        p1.save()
        
        p2 = LessonProgress.objects.create(user=self.student, lesson=self.lesson2, is_completed=True)
        p2.completed_at = now - datetime.timedelta(minutes=5)
        p2.save()
        
        # Lượt thi thử
        a1 = Attempt.objects.create(user=self.student, test=self.test_tracnghiem)
        Attempt.objects.filter(pk=a1.pk).update(
            start_time=now - datetime.timedelta(minutes=8),
            end_time=now - datetime.timedelta(minutes=7),
            total_score=9.5
        )
        
        a2 = Attempt.objects.create(user=self.student, test=self.test_python)
        Attempt.objects.filter(pk=a2.pk).update(
            start_time=now - datetime.timedelta(minutes=2)
        )
        
        # Truy cập trang chủ không có query search
        response = self.client.get(reverse('course_list'))
        self.assertEqual(response.status_code, 200)
        
        # Lấy danh sách activities truyền sang template qua context
        activities = response.context['activities']
        
        # Đảm bảo danh sách hoạt động được gộp và giới hạn tối đa là 6 hoạt động
        self.assertTrue(len(activities) <= 6)
        
        # Đảm bảo các hoạt động được sắp xếp theo thời gian giảm dần (mới nhất lên đầu)
        timestamps = [act['timestamp'] for act in activities]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))
        
        # Kiểm tra nội dung hoạt động hiển thị trong response HTML
        self.assertContains(response, "đang làm đề thi")
        self.assertContains(response, "Đề thi Python OOP")
        self.assertContains(response, "đã hoàn thành bài học")
        self.assertContains(response, "Bài 2: Biến và Hằng")
        self.assertContains(response, "đã hoàn thành đề thi")
        self.assertContains(response, "Đạt 9,5 điểm")

    def test_all_courses_view(self):
        from django.urls import reverse
        response = self.client.get(reverse('all_courses'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'lms/all_courses.html')
        
        # Đảm bảo hiển thị tất cả các khóa học
        self.assertContains(response, "Khóa học C++ Cơ bản")
        self.assertContains(response, "Lập trình Python Nâng cao")


class StudentMySpaceTestCase(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from django.utils import timezone
        from lms.models import Course, Test, CourseOwnership, TestOwnership, Lesson, LessonProgress, Attempt
        
        # 1. Tạo users
        self.student = User.objects.create_user(username="student_myspace", password="password")
        self.teacher = User.objects.create_user(username="teacher_myspace", password="password")
        
        # 2. Tạo khóa học và bài học
        self.course_owned = Course.objects.create(
            title="Khóa học C++ Đã Mua",
            description="Lập trình C++ nâng cao",
            creator=self.teacher,
            price=50000
        )
        self.lesson1 = Lesson.objects.create(
            course=self.course_owned,
            title="Bài 1: Struct",
            order_index=1,
            lesson_type="THEORY"
        )
        self.lesson2 = Lesson.objects.create(
            course=self.course_owned,
            title="Bài 2: Class",
            order_index=2,
            lesson_type="THEORY"
        )
        
        self.course_not_owned = Course.objects.create(
            title="Khóa học Java Chưa Mua",
            description="Lập trình Java từ đầu",
            creator=self.teacher,
            price=60000
        )
        
        # 3. Tạo đề thi Standalone
        self.test_owned = Test.objects.create(
            title="Đề thi C++ Đã Mua",
            duration=30,
            test_type="STANDALONE",
            price=20000
        )
        self.test_not_owned = Test.objects.create(
            title="Đề thi Java Chưa Mua",
            duration=30,
            test_type="STANDALONE",
            price=30000
        )
        
        # 4. Gán quyền sở hữu (mua) cho student
        CourseOwnership.objects.create(
            user=self.student,
            course=self.course_owned
        )
        TestOwnership.objects.create(
            user=self.student,
            test=self.test_owned,
            agreed_rules=True
        )
        
        # 5. Cho học sinh hoàn thành 1 trong 2 bài học của khóa học (50% tiến độ)
        LessonProgress.objects.create(
            user=self.student,
            lesson=self.lesson1,
            is_completed=True
        )
        
        # 6. Cho học sinh làm 2 lần đề thi C++ đã mua, lần cao nhất được 8.5 điểm
        Attempt.objects.create(
            user=self.student,
            test=self.test_owned,
            total_score=6.0,
            end_time=timezone.now()
        )
        Attempt.objects.create(
            user=self.student,
            test=self.test_owned,
            total_score=8.5,
            end_time=timezone.now()
        )

    def test_anonymous_user_redirected(self):
        from django.urls import reverse
        
        # Kiểm tra chặn truy cập /my-courses/
        response = self.client.get(reverse('my_courses'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)
        
        # Kiểm tra chặn truy cập /my-tests/
        response = self.client.get(reverse('my_tests'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_my_courses_listing_and_progress(self):
        from django.urls import reverse
        self.client.login(username="student_myspace", password="password")
        
        response = self.client.get(reverse('my_courses'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'lms/my_courses.html')
        
        # Đảm bảo hiển thị khóa học đã mua
        self.assertContains(response, "Khóa học C++ Đã Mua")
        
        # Đảm bảo KHÔNG hiển thị khóa học chưa mua
        self.assertNotContains(response, "Khóa học Java Chưa Mua")
        
        # Đảm bảo tính toán đúng tiến độ: 1/2 bài học hoàn thành => 50%
        courses_in_context = response.context['courses']
        self.assertEqual(len(courses_in_context), 1)
        self.assertEqual(courses_in_context[0]['progress_percent'], 50)
        self.assertEqual(courses_in_context[0]['completed_lessons'], 1)
        self.assertEqual(courses_in_context[0]['total_lessons'], 2)
        
        # Kiểm tra text hiển thị tiến độ và phần trăm trên giao diện
        self.assertContains(response, "50%")
        self.assertContains(response, "1")
        self.assertContains(response, "/ 2 bài")

    def test_my_tests_listing_and_scores(self):
        from django.urls import reverse
        self.client.login(username="student_myspace", password="password")
        
        response = self.client.get(reverse('my_tests'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'lms/my_tests.html')
        
        # Đảm bảo hiển thị đề thi đã mua
        self.assertContains(response, "Đề thi C++ Đã Mua")
        
        # Đảm bảo KHÔNG hiển thị đề thi chưa mua
        self.assertNotContains(response, "Đề thi Java Chưa Mua")
        
        # Đảm bảo thống kê số lượt đã thi (2 lượt) và điểm cao nhất (8.5 điểm)
        tests_in_context = response.context['tests']
        self.assertEqual(len(tests_in_context), 1)
        self.assertEqual(tests_in_context[0]['attempts_count'], 2)
        self.assertEqual(tests_in_context[0]['best_attempt'].total_score, 8.5)
        
        # Kiểm tra nội dung trên giao diện
        self.assertContains(response, "8,5")
        self.assertContains(response, "2 lần")


class CreatorAdminTrialTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from lms.models import Course, Lesson, Test
        
        self.creator = User.objects.create_user(username="course_creator", password="password")
        self.creator.profile.can_create_courses = True
        self.creator.profile.save()
        
        self.course_seq = Course.objects.create(
            title="Khóa học Tuần Tự Của Tôi",
            learning_mode="SEQUENTIAL",
            creator=self.creator
        )
        
        self.lesson1 = Lesson.objects.create(
            course=self.course_seq,
            title="Bài 1",
            order_index=1,
            lesson_type="THEORY"
        )
        self.lesson2 = Lesson.objects.create(
            course=self.course_seq,
            title="Bài 2",
            order_index=2,
            lesson_type="THEORY"
        )
        
        self.test = Test.objects.create(
            title="Đề kiểm tra của tôi",
            test_type="STANDALONE",
            price=15000,
            creator=self.creator
        )
        
        self.lesson_test = Lesson.objects.create(
            course=self.course_seq,
            title="Bài 3: Thi thử",
            order_index=3,
            lesson_type="TEST",
            test=self.test
        )

    def test_creator_bypasses_sequential_lessons(self):
        from django.urls import reverse
        self.client.login(username="course_creator", password="password")
        
        # Thử truy cập Bài 2 trực tiếp (chưa học bài 1)
        response = self.client.get(reverse('lesson_detail', args=[self.course_seq.id, self.lesson2.id]))
        # Creator được xem thử, không bị chặn (trả về 200 OK thay vì redirect 302)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bài 2")

    def test_creator_bypasses_test_ownership_and_registration(self):
        from django.urls import reverse
        self.client.login(username="course_creator", password="password")
        
        # 1. Truy cập trang detail đề thi không bị bắt đăng ký/thanh toán
        response = self.client.get(reverse('test_detail', args=[self.test.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_registered'])
        
        # 2. Bắt đầu làm bài thi không bị chặn (trả về 200 OK)
        response = self.client.get(reverse('take_test', args=[self.test.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Đề kiểm tra của tôi")

    def test_creator_dashboard_shows_created_courses_with_badge(self):
        from django.urls import reverse
        self.client.login(username="course_creator", password="password")
        
        response = self.client.get(reverse('my_courses'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'lms/my_courses.html')
        
        # Đảm bảo hiển thị khóa học tự tạo và huy hiệu Xem thử (Người tạo)
        self.assertContains(response, "Khóa học Tuần Tự Của Tôi")
        self.assertContains(response, "Xem thử (Người tạo)")

    def test_teacher_can_create_and_duplicate_test(self):
        from django.urls import reverse
        from lms.models import Test
        
        self.client.login(username="course_creator", password="password")
        
        # 1. Truy cập trang tạo đề thi
        response = self.client.get(reverse('create_test'))
        self.assertEqual(response.status_code, 200)
        
        # 2. Tạo đề thi mới bằng POST
        data = {
            'title': 'Đề thi của giáo viên',
            'price': '10000',
            'duration': '45',
            'test_type': 'STANDALONE'
        }
        response = self.client.post(reverse('create_test'), data=data)
        self.assertEqual(response.status_code, 302) # Nên chuyển hướng đến manage_test_structure
        
        new_test = Test.objects.filter(title='Đề thi của giáo viên').first()
        self.assertIsNotNone(new_test)
        self.assertEqual(new_test.creator, self.creator)
        
        # 3. Nhân đôi đề thi của chính mình
        response = self.client.post(reverse('duplicate_test', args=[new_test.id]))
        self.assertEqual(response.status_code, 302) # Nên chuyển hướng đến manage_test_structure
        
        duplicated_test = Test.objects.filter(title='[Bản sao] Đề thi của giáo viên').first()
        self.assertIsNotNone(duplicated_test)
        self.assertEqual(duplicated_test.creator, self.creator)

    def test_student_cannot_create_or_duplicate_test(self):
        from django.contrib.auth.models import User
        from django.urls import reverse
        
        # Tạo học sinh thường
        student = User.objects.create_user(username="student", password="password")
        self.client.login(username="student", password="password")
        
        # 1. Truy cập trang tạo đề thi bị chặn
        response = self.client.get(reverse('create_test'))
        self.assertEqual(response.status_code, 302) # Chuyển hướng về test_list
        
        # 2. Gửi request POST tạo đề thi bị chặn
        data = {
            'title': 'Đề thi gian lận',
            'price': '0',
            'duration': '45',
            'test_type': 'STANDALONE'
        }
        response = self.client.post(reverse('create_test'), data=data)
        self.assertEqual(response.status_code, 302)
        
        # 3. Gửi request POST nhân đôi đề thi bị chặn
        response = self.client.post(reverse('duplicate_test', args=[self.test.id]))
        self.assertEqual(response.status_code, 302)

    def test_admin_bypasses_test_ownership_and_registration(self):
        from django.contrib.auth.models import User
        from django.urls import reverse
        
        # Tạo admin/superuser
        admin_user = User.objects.create_superuser(username="admin", password="password")
        self.client.login(username="admin", password="password")
        
        # 1. Truy cập chi tiết đề thi trả phí của giáo viên khác
        response = self.client.get(reverse('test_detail', args=[self.test.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_registered'])
        
        # 2. Tiến hành làm thử đề thi
        response = self.client.get(reverse('take_test', args=[self.test.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Đề kiểm tra của tôi")


class QuestionChoicePositionTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        self.user = User.objects.create_superuser(username="admin", password="password")
        self.client.login(username="admin", password="password")

    def test_create_question_saves_custom_positions(self):
        from django.urls import reverse
        from lms.models import Question, Choice
        
        url = reverse('create_question')
        data = {
            'question_type': '1',
            'content': 'Câu hỏi 1?',
            'choice_text_1[]': ['Đáp án A', 'Đáp án B', 'Đáp án C'],
            'choice_position_1[]': ['3', '1', '2'],
            'correct_choice_1': '0',
            'is_public': 'on',
            'action': 'save'
        }
        response = self.client.post(url, data=data)
        self.assertEqual(response.status_code, 302)
        
        question = Question.objects.filter(content='Câu hỏi 1?').first()
        self.assertIsNotNone(question)
        self.assertEqual(question.question_type, 1)
        
        choices = list(Choice.objects.filter(question=question).order_by('id'))
        self.assertEqual(len(choices), 3)
        self.assertEqual(choices[0].content, 'Đáp án A')
        self.assertEqual(choices[0].position, 3)
        self.assertEqual(choices[1].content, 'Đáp án B')
        self.assertEqual(choices[1].position, 1)
        self.assertEqual(choices[2].content, 'Đáp án C')
        self.assertEqual(choices[2].position, 2)

    def test_create_question_defaults_positions_to_1(self):
        from django.urls import reverse
        from lms.models import Question, Choice
        
        url = reverse('create_question')
        data = {
            'question_type': '1',
            'content': 'Câu hỏi 2?',
            'choice_text_1[]': ['Đáp án A', 'Đáp án B'],
            'choice_position_1[]': ['', ' '],  # Trống
            'correct_choice_1': '0',
            'is_public': 'on',
            'action': 'save'
        }
        response = self.client.post(url, data=data)
        self.assertEqual(response.status_code, 302)
        
        question = Question.objects.filter(content='Câu hỏi 2?').first()
        choices = list(Choice.objects.filter(question=question).order_by('id'))
        self.assertEqual(len(choices), 2)
        self.assertEqual(choices[0].position, 1)
        self.assertEqual(choices[1].position, 1)

    def test_edit_question_updates_positions_correctly(self):
        from django.urls import reverse
        from lms.models import Question, Choice
        
        # 1. Tạo câu hỏi trước
        question = Question.objects.create(
            content='Câu hỏi 3?',
            question_type=2,
            is_public=True,
            creator=self.user
        )
        c1 = Choice.objects.create(question=question, content='A', is_correct=True, position=1)
        c2 = Choice.objects.create(question=question, content='B', is_correct=False, position=1)
        
        # 2. Edit thông qua POST
        url = reverse('edit_question', args=[question.id])
        data = {
            'question_type': '2',
            'content': 'Câu hỏi 3 đã sửa?',
            'choice_text_2[]': ['A', 'B', 'C'],
            'choice_position_2[]': ['10', '5', '8'],
            'correct_choices_2[]': ['0', '2'],
            'is_public': 'on',
            'action': 'save'
        }
        response = self.client.post(url, data=data)
        self.assertEqual(response.status_code, 302)
        
        question.refresh_from_db()
        self.assertEqual(question.content, 'Câu hỏi 3 đã sửa?')
        
        choices = list(Choice.objects.filter(question=question).order_by('id'))
        self.assertEqual(len(choices), 3)
        self.assertEqual(choices[0].content, 'A')
        self.assertEqual(choices[0].position, 10)
        self.assertEqual(choices[0].is_correct, True)
        self.assertEqual(choices[1].content, 'B')
        self.assertEqual(choices[1].position, 5)
        self.assertEqual(choices[1].is_correct, False)
        self.assertEqual(choices[2].content, 'C')
        self.assertEqual(choices[2].position, 8)
        self.assertEqual(choices[2].is_correct, True)


class TestShufflePartsTestCase(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from lms.models import Test, Question, TestQuestion
        
        self.creator = User.objects.create_superuser(username="admin_shuffle", password="password")
        self.client.login(username="admin_shuffle", password="password")
        
        # Create a Test
        self.test = Test.objects.create(
            title="Đề thi 2 phần",
            price=0.0,
            duration=60,
            shuffle_parts=True,
            creator=self.creator
        )
        
        # Create questions
        self.q1 = Question.objects.create(content="Câu hỏi phần 1", question_type=1)
        self.q2 = Question.objects.create(content="Câu hỏi phần 2", question_type=1)
        
        # Add to test questions
        TestQuestion.objects.create(test=self.test, question=self.q1, part_number=1, order_index=1)
        TestQuestion.objects.create(test=self.test, question=self.q2, part_number=2, order_index=1)

    def test_shuffle_parts_shuffles_and_preserves_order(self):
        from django.urls import reverse
        from lms.models import Attempt
        
        # Start attempt
        response = self.client.get(reverse('take_test', args=[self.test.id]))
        self.assertEqual(response.status_code, 200)
        
        attempt = Attempt.objects.get(user=self.creator, test=self.test)
        self.assertIn('_part_order', attempt.shuffled_data)
        
        # Ensure '_part_order' has both parts
        part_order = attempt.shuffled_data['_part_order']
        self.assertEqual(set(part_order), {'1', '2'})
        
        # Verify that it renders correctly in review view too
        response = self.client.get(reverse('review_attempt', args=[attempt.id]))
        self.assertEqual(response.status_code, 200)

    def test_shuffle_parts_false_enforces_natural_order(self):
        from django.urls import reverse
        from lms.models import Attempt
        
        # Set shuffle_parts to False
        self.test.shuffle_parts = False
        self.test.save()
        
        # Start attempt
        response = self.client.get(reverse('take_test', args=[self.test.id]))
        self.assertEqual(response.status_code, 200)
        
        # Manually alter the attempt's _part_order to be reversed
        attempt = Attempt.objects.get(user=self.creator, test=self.test)
        attempt.shuffled_data['_part_order'] = ['2', '1']
        attempt.save()
        
        # Fetch the take_test view and verify the rendered parts are ordered ['1', '2']
        response = self.client.get(reverse('take_test', args=[self.test.id]))
        self.assertEqual(response.status_code, 200)
        parts = response.context['parts']
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0]['part']['id'], 1)
        self.assertEqual(parts[1]['part']['id'], 2)
        
        # Verify review_attempt view also gets them in natural sequential order ['1', '2']
        response = self.client.get(reverse('review_attempt', args=[attempt.id]))
        self.assertEqual(response.status_code, 200)
        parts = response.context['parts']
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0]['part']['id'], 1)
        self.assertEqual(parts[1]['part']['id'], 2)


class TestQuestionsQuickAddTestCase(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from lms.models import Test, Question
        
        self.creator = User.objects.create_superuser(username="admin_quick_add", password="password")
        self.client.login(username="admin_quick_add", password="password")
        
        self.test = Test.objects.create(
            title="Đề thi test thêm nhanh",
            price=0.0,
            duration=60,
            creator=self.creator
        )
        
        # Tạo 10 câu hỏi để test
        self.questions = []
        for i in range(10):
            q = Question.objects.create(content=f"Câu hỏi test {i+1}", question_type=1)
            self.questions.append(q)

    def test_add_questions_quick_ranges(self):
        from django.urls import reverse
        from lms.models import TestQuestion
        
        # Test range tăng dần, có khoảng trắng, dấu phẩy, tab, dòng mới...
        # Giả sử IDs câu hỏi bắt đầu từ ID của self.questions[0].id
        base_id = self.questions[0].id
        
        # Ví dụ: base_id (đơn lẻ), base_id+1-base_id+3 (range tăng), base_id+7-base_id+5 (range giảm), base_id+9 (đơn lẻ)
        # Chuỗi: f"{base_id} {base_id+1}-{base_id+3}, {base_id+7} - {base_id+5}\n{base_id+9}"
        raw_ids = f"{base_id} {base_id+1}-{base_id+3}, {base_id+7} - {base_id+5}\n{base_id+9}"
        
        response = self.client.post(
            reverse('add_questions_quick_ajax', args=[self.test.id]),
            data={'ids': raw_ids},
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        
        # Kiểm tra các câu hỏi đã được thêm
        # Thứ tự mong muốn: base_id (0), base_id+1 (1), base_id+2 (2), base_id+3 (3), base_id+7 (7), base_id+6 (6), base_id+5 (5), base_id+9 (9)
        added_q_ids = [tq.question.id for tq in TestQuestion.objects.filter(test=self.test).order_by('id')]
        expected_ids = [
            base_id,
            base_id+1,
            base_id+2,
            base_id+3,
            base_id+7,
            base_id+6,
            base_id+5,
            base_id+9
        ]
        self.assertEqual(added_q_ids, expected_ids)

    def test_add_questions_quick_range_too_large(self):
        from django.urls import reverse
        
        # Thử với range quá lớn (lớn hơn 500)
        raw_ids = "1-502"
        response = self.client.post(
            reverse('add_questions_quick_ajax', args=[self.test.id]),
            data={'ids': raw_ids},
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("quá lớn", data['error'])


class QuestionImportDirectToTestTestCase(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from lms.models import Test
        
        self.user = User.objects.create_superuser(username="admin_import", password="password")
        self.client.login(username="admin_import", password="password")
        
        self.test = Test.objects.create(
            title="Đề thi import trực tiếp",
            price=0.0,
            duration=60,
            creator=self.user
        )

    def test_save_import_ajax_with_test_id(self):
        from django.urls import reverse
        from lms.models import Question, Choice, TestQuestion
        
        import_data = {
            'questions': [
                {
                    'content': 'Thủ đô của Việt Nam?',
                    'type': 1,
                    'choices': [
                        {'content': 'Hà Nội', 'is_correct': True},
                        {'content': 'Hồ Chí Minh', 'is_correct': False}
                    ],
                    'valid': True
                },
                {
                    'content': 'Các tỉnh Tây Nguyên?',
                    'type': 2,
                    'choices': [
                        {'content': 'Lâm Đồng', 'is_correct': True},
                        {'content': 'Cần Thơ', 'is_correct': False}
                    ],
                    'valid': True
                }
            ],
            'group_id': None,
            'test_id': self.test.id,
            'default_points': 0.5
        }
        
        # Test POST request to save import
        response = self.client.post(
            reverse('save_import_ajax'),
            data=import_data,
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['count'], 2)
        
        # Verify TestQuestions are created and linked to the test
        tqs = list(TestQuestion.objects.filter(test=self.test).order_by('order_index'))
        self.assertEqual(len(tqs), 2)
        
        # First question check
        self.assertEqual(tqs[0].question.content, 'Thủ đô của Việt Nam?')
        self.assertEqual(tqs[0].order_index, 1)
        self.assertEqual(tqs[0].part_number, 1)
        self.assertEqual(tqs[0].points, 0.5)
        
        # Second question check
        self.assertEqual(tqs[1].question.content, 'Các tỉnh Tây Nguyên?')
        self.assertEqual(tqs[1].order_index, 1)
        self.assertEqual(tqs[1].part_number, 1)
        self.assertEqual(tqs[1].points, 0.5)

    def test_create_question_with_test_id(self):
        from django.urls import reverse
        from lms.models import Question, Choice, TestQuestion
        
        # Test GET request
        response = self.client.get(reverse('create_question') + f"?test_id={self.test.id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['test'], self.test)
        self.assertEqual(response.context['test_id'], str(self.test.id))
        
        # Test POST request to create question and link it to test
        data = {
            'question_type': '1',
            'content': 'Câu hỏi được thêm trực tiếp?',
            'choice_text_1[]': ['Đáp án A', 'Đáp án B'],
            'choice_position_1[]': ['1', '2'],
            'correct_choice_1': '0',
            'is_public': 'on',
            'action': 'save'
        }
        
        response = self.client.post(
            reverse('create_question') + f"?test_id={self.test.id}",
            data=data
        )
        # Should redirect to manage_test_structure page
        self.assertRedirects(response, reverse('manage_test_structure', args=[self.test.id]))
        
        # Verify question and choice were created
        question = Question.objects.filter(content='Câu hỏi được thêm trực tiếp?').first()
        self.assertIsNotNone(question)
        
        # Verify linked TestQuestion exists
        tq = TestQuestion.objects.filter(test=self.test, question=question).first()
        self.assertIsNotNone(tq)
        self.assertEqual(tq.points, 1.0)
        self.assertEqual(tq.part_number, 1)




