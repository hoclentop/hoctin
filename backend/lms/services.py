import requests
from bs4 import BeautifulSoup
from django.utils import timezone
from .models import Attempt, AttemptAnswer, Choice, TestQuestion

class ScoringService:
    @staticmethod
    def calculate_attempt_score(attempt):
        """Tính toán tổng điểm cho một lượt làm bài."""
        total_score = 0
        is_dynamic = hasattr(attempt.test, 'dynamictest')

        if is_dynamic:
            # Lấy tất cả câu trả lời của lượt thi này cho đề động
            answers = attempt.answers.select_related('question').all()
            for ans in answers:
                ans.score_earned = ScoringService.score_question(ans)
                ans.save()
                total_score += ans.score_earned
        else:
            parts_data = {} # Để kiểm tra logic TC1/TC2
            
            # Lấy tất cả câu trả lời của lượt thi này
            answers = attempt.answers.select_related('test_question').all()
            
            # Phân loại câu trả lời theo phần thi (Part Number)
            for ans in answers:
                if not ans.test_question:
                    continue
                part_id = ans.test_question.part_number
                if part_id not in parts_data:
                    parts_data[part_id] = {'TC1': False, 'TC2': False, 'answers': []}
                
                # Đánh dấu nếu học viên đã làm TC1 hoặc TC2
                if ans.test_question.optional_type == 'TC1':
                    # Với TC1/TC2, ta coi là "đã làm" nếu có chọn đáp án hoặc điền text
                    if ans.selected_choices.exists() or ans.short_answer:
                        parts_data[part_id]['TC1'] = True
                elif ans.test_question.optional_type == 'TC2':
                    if ans.selected_choices.exists() or ans.short_answer:
                        parts_data[part_id]['TC2'] = True
                
                parts_data[part_id]['answers'].append(ans)

            # Chấm điểm từng phần
            for part_id, data in parts_data.items():
                # QUY TẮC: Nếu làm cả TC1 và TC2 -> 0 điểm cho tất cả câu hỏi tự chọn trong phần đó
                both_attempted = data['TC1'] and data['TC2']
                
                for ans in data['answers']:
                    if both_attempted and ans.test_question.optional_type in ['TC1', 'TC2']:
                        ans.score_earned = 0
                    else:
                        ans.score_earned = ScoringService.score_question(ans)
                    
                    ans.save()
                    total_score += ans.score_earned
        
        attempt.total_score = total_score
        attempt.end_time = timezone.now()
        attempt.save()
        return total_score

    @staticmethod
    def score_question(answer):
        """Tính điểm cho một câu hỏi cụ thể."""
        if answer.test_question:
            q = answer.test_question.question
            max_points = answer.test_question.points
        else:
            q = answer.question
            max_points = answer.points
            
        if not q:
            return 0
        
        if q.question_type == 1: # Single Choice
            selected = answer.selected_choices.first()
            if selected and selected.is_correct:
                return max_points
                
        elif q.question_type == 2: # Multiple Choice
            correct_choices = set(q.choices.filter(is_correct=True).values_list('id', flat=True))
            selected_choices = set(answer.selected_choices.values_list('id', flat=True))
            if correct_choices == selected_choices and correct_choices:
                return max_points
                
        elif q.question_type == 3: # Đúng/Sai 4 ý (Tiered Scoring)
            # Giả sử selected_choices chứa các ý mà học viên chọn là "Đúng"
            # Trong thực tế, loại 3 cần lưu vết cả Đúng/Sai cho từng ý. 
            # Ở đây ta dùng is_correct_tf (JSON list) lưu index các ý đúng mà học viên chọn.
            # k là số lượng ý trả lời chính xác (khớp với đáp án)
            
            correct_count = 0
            all_choices = list(q.choices.all()) # Giả sử có đúng 4 ý
            selected_ids = set(answer.selected_choices.values_list('id', flat=True))
            
            # Lưu ý: Với loại 3, mỗi Choice đại diện cho 1 ý. 
            # Học viên trả lời bằng cách tích chọn nếu ý đó Đúng, để trống nếu ý đó Sai.
            for choice in all_choices:
                is_selected = choice.id in selected_ids
                if is_selected == choice.is_correct:
                    correct_count += 1
            
            # Barem phân tầng (100%, 50%, 25%, 10%)
            if correct_count == 4:
                return max_points
            elif correct_count == 3:
                return 0.5 * max_points
            elif correct_count == 2:
                return 0.25 * max_points
            elif correct_count == 1:
                return 0.1 * max_points
            return 0
            
        elif q.question_type == 4: # Short Answer
            user_val = answer.short_answer.strip()
            correct_options = q.choices.all()
            for opt in correct_options:
                u_val = user_val
                target = opt.content.strip()
                
                # Tận dụng hai trường sẵn có cho câu trả lời ngắn:
                # opt.is_correct: True/False đánh dấu Phân biệt/Không phân biệt chữ hoa-thường
                # opt.position: 1/0 đánh dấu Bỏ qua/Không bỏ qua dấu cách
                is_case_sensitive = opt.is_correct
                ignore_spaces = (opt.position == 1)
                
                if ignore_spaces:
                    u_val = "".join(u_val.split())
                    target = "".join(target.split())
                
                if is_case_sensitive:
                    if u_val == target: return max_points
                else:
                    if u_val.lower() == target.lower(): return max_points
                    
        return 0

class JudgeSyncService:
    @staticmethod
    def _sync_dmoj_platform(base_url, username, problem_code):
        """Helper to sync from DMOJ-based platforms like VNOJ or on.hsgtin.vn."""
        # 1. Thử gọi API bảo mật nếu có cấu hình VNOJ_API_TOKEN trong settings
        from django.conf import settings
        api_token = getattr(settings, 'VNOJ_API_TOKEN', None)
        if api_token:
            api_url = f"{base_url.rstrip('/')}/api/lms-check-submission/"
            try:
                headers = {
                    'Authorization': f'Bearer {api_token}',
                    'User-Agent': 'Mozilla/5.0'
                }
                params = {
                    'user': username,
                    'problem': problem_code
                }
                resp = requests.get(api_url, params=params, headers=headers, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get('success'):
                        return data.get('has_ac', False)
            except Exception:
                # Nếu API gặp sự cố (ví dụ endpoint chưa được viết bên VNOJ), tự động fallback xuống cào HTML
                pass

        # 2. Phương án cào HTML (Chỉ hoạt động đối với bài tập công khai)
        url = f"{base_url.rstrip('/')}/submissions/user/{username}/?status=AC"
        try:
            resp = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            if resp.status_code != 200:
                return False
            
            soup = BeautifulSoup(resp.text, 'html.parser')
            rows = soup.find_all('div', class_='submission-row')
            for row in rows:
                # Get username to be absolutely sure
                user_span = row.find('span', class_='user')
                if user_span:
                    row_user = user_span.get_text(strip=True)
                    if row_user.lower() != username.lower():
                        continue
                
                # Get problem code from name link
                name_div = row.find('div', class_='name')
                if not name_div:
                    continue
                a_link = name_div.find('a')
                if not a_link:
                    continue
                href = a_link.get('href', '')
                if '/problem/' not in href:
                    continue
                
                # Extract problem code (e.g. "/problem/hsg7d25b4" -> "hsg7d25b4")
                prob_code = href.split('/problem/')[-1].strip('/')
                if prob_code.lower() == problem_code.lower():
                    # Double check status is AC
                    status_span = row.find('span', class_='status')
                    result_div = row.find('div', class_='sub-result')
                    
                    status_text = status_span.get_text(strip=True) if status_span else ""
                    result_classes = result_div.get('class', []) if result_div else []
                    
                    if status_text == 'AC' or 'AC' in result_classes:
                        return True
            return False
        except Exception:
            return False

    @staticmethod
    def sync_vnoj(username, problem_code):
        """Sync via VNOJ scraping."""
        return JudgeSyncService._sync_dmoj_platform("https://oj.vnoi.info", username, problem_code)

    @staticmethod
    def sync_hsgtin(username, problem_code):
        """Sync via on.hsgtin.vn scraping (using HTTP)."""
        return JudgeSyncService._sync_dmoj_platform("http://on.hsgtin.vn", username, problem_code)

    @staticmethod
    def sync_codeforces(username, problem_code):
        """Sync via Codeforces API."""
        # API: https://codeforces.com/api/user.status?handle={username}
        url = f"https://codeforces.com/api/user.status?handle={username}&from=1&count=50"
        try:
            resp = requests.get(url, timeout=10).json()
            if resp['status'] == 'OK':
                for sub in resp['result']:
                    # Problem code thường dạng "123A"
                    p = sub['problem']
                    p_code = f"{p['contestId']}{p['index']}"
                    if p_code == problem_code and sub['verdict'] == 'OK':
                        return True
            return False
        except:
            return False
