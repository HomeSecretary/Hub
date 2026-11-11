// Secretary 허브 빌드·배포 파이프라인
//
// Jenkins 컨테이너에는 docker가 없다. 빌드와 배포는 배포 서버(NAS)가 직접 한다.
//   Gitea push → webhook → Jenkins
//     1) Upload  : 이 커밋의 소스를 SSH로 배포 서버에 올린다 (git archive)
//     2) Test    : 배포 서버에서 테스트 이미지 빌드 + pytest
//     3) Deploy  : main 브랜치면 .env를 올리고 빌드 → 배포 → 헬스체크 (실패하면 직전 이미지로)
//
// 필요한 Jenkins 플러그인: SSH Agent
//
// 필요한 Jenkins Credentials:
//   (SSH Username with private key)  배포 서버 접속용 개인키. ID는 SECRETARY_SSH_CREDENTIALS 로 지정
//   secretary-env (Secret file)  이 저장소의 .env 파일 전체 (.env.example 참고)
//
// 설치마다 다른 값은 저장소에 적지 않고 Jenkins 환경 변수로 넣는다
// (Manage Jenkins → System → Global properties → Environment variables, 또는 작업 설정):
//   SECRETARY_NAS_HOST         필수. 접속 계정@주소. 예: deploy@192.168.0.10
//   SECRETARY_SSH_CREDENTIALS  필수. 위 개인키 Credential의 ID
//   SECRETARY_NAS_BASE         선택. 기본 /volume1/docker/secretary (src/hub/, data/ 가 여기 생긴다)
//   SECRETARY_NAS_DOCKER       선택. 기본 docker. 배포 계정이 docker 그룹이 아니면 "sudo docker"
//
// 호스트 키는 미리 Jenkins의 known_hosts 에 등록해 둔다 (StrictHostKeyChecking=yes).

pipeline {
    agent any

    options {
        disableConcurrentBuilds()
        timeout(time: 20, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }

    environment {
        NAS_HOST        = "${env.SECRETARY_NAS_HOST ?: ''}"
        SSH_CREDENTIALS = "${env.SECRETARY_SSH_CREDENTIALS ?: ''}"
        NAS_BASE        = "${env.SECRETARY_NAS_BASE ?: '/volume1/docker/secretary'}"
        NAS_DOCKER      = "${env.SECRETARY_NAS_DOCKER ?: 'docker'}"
        SSH_OPTS        = '-o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10'
    }

    stages {
        stage('Prepare') {
            steps {
                script {
                    def missing = ['SECRETARY_NAS_HOST': env.NAS_HOST, 'SECRETARY_SSH_CREDENTIALS': env.SSH_CREDENTIALS]
                        .findAll { k, v -> !v?.trim() }.keySet()
                    if (missing) {
                        error("Jenkins 환경 변수를 설정하세요: ${missing.join(', ')}")
                    }
                    env.IMAGE_TAG = sh(script: 'git rev-parse --short=12 HEAD', returnStdout: true).trim()
                    currentBuild.description = "${env.IMAGE_TAG}"
                }
            }
        }

        stage('Upload') {
            steps {
                sshagent(credentials: [env.SSH_CREDENTIALS]) {
                    // 작은따옴표 셸 스크립트: Groovy가 아니라 셸이 변수를 해석하게 해서 비밀값이 새지 않게 한다.
                    sh '''
                        git archive --format=tar HEAD \
                          | ssh $SSH_OPTS "$NAS_HOST" "rm -rf '$NAS_BASE/src/hub' && mkdir -p '$NAS_BASE/src/hub' && tar -x -C '$NAS_BASE/src/hub'"
                    '''
                }
            }
        }

        stage('Test') {
            steps {
                sshagent(credentials: [env.SSH_CREDENTIALS]) {
                    sh '''
                        ssh $SSH_OPTS "$NAS_HOST" "IMAGE_TAG='$IMAGE_TAG' DOCKER='$NAS_DOCKER' bash '$NAS_BASE/src/hub/deploy/deploy.sh' test"
                    '''
                }
            }
        }

        stage('Deploy') {
            when {
                expression { env.GIT_BRANCH in ['main', 'origin/main'] }
            }
            steps {
                sshagent(credentials: [env.SSH_CREDENTIALS]) {
                    withCredentials([file(credentialsId: 'secretary-env', variable: 'ENV_FILE')]) {
                        sh '''
                            # .env는 표준 입력으로 보낸다. 명령줄이나 로그에 값이 남지 않는다. 배포 스크립트가 끝나면 지운다.
                            ssh $SSH_OPTS "$NAS_HOST" "umask 077 && cat > '$NAS_BASE/src/hub/.env'" < "$ENV_FILE"
                            ssh $SSH_OPTS "$NAS_HOST" "IMAGE_TAG='$IMAGE_TAG' DOCKER='$NAS_DOCKER' SECRETARY_DATA_DIR_HOST='$NAS_BASE/data' bash '$NAS_BASE/src/hub/deploy/deploy.sh' deploy"
                        '''
                    }
                }
            }
        }
    }

    post {
        success {
            echo "완료: ${env.IMAGE_TAG}"
        }
    }
}
