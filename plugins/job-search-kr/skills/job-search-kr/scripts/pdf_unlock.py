"""암호 걸린 PDF를 암호 없이 저장한다. 암호는 화면에 보이지 않게 입력받고 어디에도 남기지 않는다.
사용: python pdf_unlock.py 원본.pdf 결과.pdf"""
import sys, getpass, pikepdf

src, dst = sys.argv[1], sys.argv[2]
pw = getpass.getpass('PDF 암호 입력(화면에 안 보임): ')
try:
    with pikepdf.open(src, password=pw) as pdf:
        pdf.save(dst)  # 암호화 옵션 없이 저장 → 암호 해제
except pikepdf.PasswordError:
    sys.exit('암호가 맞지 않습니다. 다시 실행하세요.')
print('완료:', dst)
