#!/bin/sh
# GitHub PAT 인증용 GIT_ASKPASS 스크립트. git이 HTTPS push 시 자격증명을
# 물어볼 때(Username/Password prompt) 이 스크립트를 호출한다 - 어떤 프롬프트든
# 토큰을 그대로 돌려준다(GitHub는 PAT를 password 자리에 쓰면 username은 안 봄).
echo "$GIT_TOKEN"
