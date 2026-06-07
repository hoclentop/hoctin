#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import re
import argparse
import unicodedata
import difflib
from collections import defaultdict

# Setup Django environment
def setup_django():
    # Add backend directory to sys.path
    base_dir = os.path.dirname(os.path.abspath(__file__))
    backend_dir = os.path.join(base_dir, 'backend')
    sys.path.append(backend_dir)
    
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core_project.settings')
    try:
        import django
        django.setup()
    except ImportError as exc:
        print("Lỗi: Không thể import Django. Vui lòng đảm bảo bạn đang chạy script trong virtual environment phù hợp.")
        print("Ví dụ: .\\venv\\Scripts\\python.exe detect_duplicates.py 100 200")
        sys.exit(1)

def clean_text(text):
    if not text:
        return ""
    # Chuẩn hóa unicode tiếng Việt (tránh lỗi cùng một chữ nhưng tổ hợp khác nhau)
    text = unicodedata.normalize('NFC', text)
    # Chuyển thành chữ thường
    text = text.lower()
    
    # Loại bỏ thẻ HTML
    text = re.sub(r'<[^>]*>', ' ', text)
    
    # Loại bỏ liên kết markdown [text](url) -> text
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    
    # Loại bỏ ký tự định dạng markdown nhưng giữ nguyên nội dung bên trong
    text = re.sub(r'[\*\_\`\~\#]', ' ', text)
    
    # Loại bỏ các dấu câu thông thường nhưng GIỮ LẠI các toán tử toán học và ký tự đặc biệt cần cho biểu thức (+ - * / = < > % ^ $)
    text = re.sub(r'[\,\.\?\!\;\:\(\)\[\]\{\}\"\'\“\”\‘\’\\\|]', ' ', text)
    
    # Chuẩn hóa khoảng trắng
    text = re.sub(r'\r', ' ', text)
    text = re.sub(r'\n', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    
    return text.strip()

def find_connected_components(vertices, edges):
    """
    Tìm các thành phần liên thông trong đồ thị vô hướng.
    Trả về danh sách các nhóm đỉnh liên thông, mỗi nhóm là một danh sách đã sắp xếp.
    """
    adj = defaultdict(list)
    for u, v in edges:
        adj[u].append(v)
        adj[v].append(u)
        
    visited = set()
    components = []
    
    for v in vertices:
        if v not in visited:
            # Bắt đầu BFS từ v
            component = []
            queue = [v]
            visited.add(v)
            
            while queue:
                curr = queue.pop(0)
                component.append(curr)
                for neighbor in adj[curr]:
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append(neighbor)
            
            if len(component) > 1:
                components.append(sorted(component))
                
    return components

def main():
    parser = argparse.ArgumentParser(
        description="Script phát hiện các câu hỏi trùng nhau trong hệ thống HocTin LMS.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("start_id", type=int, help="ID bắt đầu của dải câu hỏi cần xét.")
    parser.add_argument("end_id", type=int, help="ID kết thúc của dải câu hỏi cần xét.")
    parser.add_argument(
        "-t", "--threshold", 
        type=float, 
        default=0.85, 
        help="Độ tương đồng tối thiểu để coi là trùng nhau (từ 0.0 đến 1.0, mặc định: 0.85)."
    )
    parser.add_argument(
        "--exact", 
        action="store_true", 
        help="Chỉ quét trùng lặp tuyệt đối (tương đương với --threshold 1.0)."
    )
    parser.add_argument(
        "--verbose", 
        action="store_true", 
        help="Hiển thị thông tin chi tiết quá trình quét."
    )
    
    args = parser.parse_args()
    
    if args.start_id > args.end_id:
        print("Lỗi: ID bắt đầu phải nhỏ hơn hoặc bằng ID kết thúc.")
        sys.exit(1)
        
    threshold = 1.0 if args.exact else args.threshold
    if not (0.0 <= threshold <= 1.0):
        print("Lỗi: Độ tương đồng phải nằm trong khoảng từ 0.0 đến 1.0.")
        sys.exit(1)
        
    setup_django()
    
    from lms.models import Question
    
    if args.verbose:
        print(f"Đang tải các câu hỏi có ID từ {args.start_id} đến {args.end_id}...")
        
    # Lấy danh sách câu hỏi trong dải ID
    questions = list(Question.objects.filter(id__range=(args.start_id, args.end_id)).only('id', 'content'))
    
    if not questions:
        print(f"Không tìm thấy câu hỏi nào trong dải ID từ {args.start_id} đến {args.end_id}.")
        return
        
    if args.verbose:
        print(f"Đã tải {len(questions)} câu hỏi. Đang tiền xử lý văn bản...")
        
    # Tiền xử lý văn bản và lưu lại
    cleaned_data = {}
    for q in questions:
        cleaned_data[q.id] = clean_text(q.content)
        
    # Để tối ưu hóa, đầu tiên ta nhóm các câu hỏi trùng khít 100% văn bản đã làm sạch
    exact_groups = defaultdict(list)
    for q_id, text in cleaned_data.items():
        exact_groups[text].append(q_id)
        
    edges = []
    
    # Các câu hỏi đã trùng khít 100% thì chắc chắn có liên kết với nhau
    for group in exact_groups.values():
        if len(group) > 1:
            for i in range(len(group) - 1):
                edges.append((group[i], group[i+1]))
                
    # Đại diện của mỗi nhóm trùng khít 100% sẽ được so sánh mờ với các đại diện nhóm khác
    representatives = []
    rep_to_group = {}
    for text, group in exact_groups.items():
        # Lấy phần tử đầu tiên làm đại diện cho nhóm
        rep_id = group[0]
        representatives.append(rep_id)
        rep_to_group[rep_id] = group
        
    if args.verbose:
        print(f"Nhóm trùng khít 100%: Tìm thấy {len(questions) - len(representatives)} câu hỏi trùng khít hoàn toàn.")
        print(f"Bắt đầu so sánh tương đồng mờ trên {len(representatives)} đại diện (ngưỡng similarity >= {threshold})...")
        
    # So sánh các đại diện với nhau bằng SequenceMatcher
    num_reps = len(representatives)
    
    # Tính toán hệ số chiều dài tối thiểu để có thể đạt độ tương đồng mong muốn:
    # L1 / L2 >= threshold / (2 - threshold) với L1 <= L2
    len_factor_threshold = threshold / (2.0 - threshold) if threshold < 2.0 else 1.0
    
    for i in range(num_reps):
        rep1_id = representatives[i]
        text1 = cleaned_data[rep1_id]
        len1 = len(text1)
        if len1 == 0:
            continue
            
        for j in range(i + 1, num_reps):
            rep2_id = representatives[j]
            text2 = cleaned_data[rep2_id]
            len2 = len(text2)
            if len2 == 0:
                continue
                
            # Kiểm tra tỷ lệ độ dài trước để bỏ qua các so sánh không thể đạt ngưỡng
            min_len = min(len1, len2)
            max_len = max(len1, len2)
            if min_len / max_len < len_factor_threshold:
                continue
                
            # So sánh độ tương đồng bằng difflib
            matcher = difflib.SequenceMatcher(None, text1, text2)
            # Dùng quick_ratio() trước để loại bỏ nhanh các chuỗi quá khác nhau
            if matcher.quick_ratio() < threshold:
                continue
                
            ratio = matcher.ratio()
            if ratio >= threshold:
                # Thêm liên kết giữa đại diện nhóm 1 và đại diện nhóm 2
                edges.append((rep1_id, rep2_id))
                if args.verbose:
                    print(f"  [Trùng mờ] ID {rep1_id} <-> ID {rep2_id} (Độ tương đồng: {ratio:.2%})")
                    
    # Tìm các nhóm trùng (thành phần liên thông)
    question_ids = [q.id for q in questions]
    duplicate_groups = find_connected_components(question_ids, edges)
    
    # In kết quả theo yêu cầu
    if args.verbose:
        print(f"\n--- KẾT QUẢ QUÉT (Tìm thấy {len(duplicate_groups)} nhóm có khả năng trùng) ---")
        
    # Sắp xếp các nhóm theo ID nhỏ nhất của mỗi nhóm
    duplicate_groups.sort(key=lambda g: g[0])
    
    for group in duplicate_groups:
        # In danh sách ID của các câu hỏi trùng nhau, ngăn cách bằng dấu phẩy
        print(", ".join(map(str, group)))

if __name__ == "__main__":
    main()
