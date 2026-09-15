#!/bin/sh
# GitHub PAT 인증용 GIT_ASKPASS 스크립트. git이 HTTPS push 시 자격증명을
# 물어볼 때 $1에 실제 프롬프트 문구("Username for '...'"/"Password for '...'")가
# 들어온다. 실측(2026-09-15): 두 프롬프트 모두에 토큰을 돌려주면
# username=password=토큰 조합이 되어 GitHub가 "Invalid username or token"으로
# 거부함 - Username에는 고정 문자열(x-access-token, GitHub Actions 등에서
# 쓰는 관례), Password에만 실제 토큰을 돌려줘야 함.
case "$1" in
  Username*) echo "x-access-token" ;;
  *) echo "$GIT_TOKEN" ;;
esac
