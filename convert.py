#xml -> txt 파일 만들기
import os
import xml.etree.ElementTree as ET

def convert_xml_to_txt(xml_file, output_dir):
    # XML 파일 파싱
    tree = ET.parse(xml_file)
    root = tree.getroot()

    # XML 파일 이름과 같은 이름의 TXT 파일 생성
    base_name = os.path.basename(xml_file)
    txt_file_name = os.path.splitext(base_name)[0] + ".txt"
    txt_file_path = os.path.join(output_dir, txt_file_name)

    with open(txt_file_path, 'w') as txt_file:
        for obj in root.findall('object'):
            # x0, y0, x1, y1, x2, y2, x3, y3, surname, difficulty
            robndbox = obj.find('robndbox')
            x0 = robndbox.find('x0').text
            y0 = robndbox.find('y0').text
            x1 = robndbox.find('x1').text
            y1 = robndbox.find('y1').text
            x2 = robndbox.find('x2').text
            y2 = robndbox.find('y2').text
            x3 = robndbox.find('x3').text
            y3 = robndbox.find('y3').text
            subname = obj.find('subname').text

            row = f"{x0},{y0},{x1},{y1},{x2},{y2},{x3},{y3},{subname},0\n"
            txt_file.write(row)

input_dir = '/workspace/dataset/SAT-MTB_Dataset/airplane/02/det/OBB'  # XML 파일들이 위치한 디렉토리
output_dir = '/workspace/dataset/SAT-MTB_Dataset/airplane/02/labelTxt'  # 변환된 TXT 파일을 저장할 디렉토리

if not os.path.exists(output_dir):
    os.makedirs(output_dir)

for xml_file in os.listdir(input_dir):
    if xml_file.endswith('.xml'):
        convert_xml_to_txt(os.path.join(input_dir, xml_file), output_dir)
