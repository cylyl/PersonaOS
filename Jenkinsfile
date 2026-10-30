pipeline {
    agent any

    environment {
        REGISTRY = '192.168.1.239:5000'
        IMAGE_NAME = 'personaos'
        IMAGE_FULL = "${REGISTRY}/${IMAGE_NAME}"
        K3S_MASTER = '192.168.1.150'
    }

    stages {
        stage('Compute Image Tag') {
            steps {
                script {
                    env.GIT_COMMIT_SHORT = sh(
                        script: "git rev-parse --short HEAD 2>/dev/null || echo 'unknown'",
                        returnStdout: true
                    ).trim()
                    env.IMAGE_TAG = "${env.BUILD_NUMBER}-${env.GIT_COMMIT_SHORT}"
                    echo "Deploying PersonaOS image tag: ${env.IMAGE_TAG}"
                }
            }
        }

        stage('Build Docker Image') {
            steps {
                sh """
                    docker build \
                        -t ${IMAGE_FULL}:${IMAGE_TAG} \
                        -t ${IMAGE_FULL}:latest \
                        -f Dockerfile \
                        .
                """
            }
        }

        stage('Push Docker Image to Local Registry') {
            steps {
                sh """
                    docker push ${IMAGE_FULL}:${IMAGE_TAG}
                    docker push ${IMAGE_FULL}:latest
                """
            }
        }

        stage('Deploy to K3s Cluster (Site 2 / PVE2)') {
            steps {
                script {
                    sh """
                        # Apply Kubernetes manifests via K3s control-plane node (k3s01)
                        ssh -o StrictHostKeyChecking=no user@${K3S_MASTER} "\
                            sudo kubectl apply -k https://github.com/cylyl/PersonaOS.git/deploy/k8s?ref=main || \
                            (sudo kubectl apply -f - << 'EOF'
$(cat deploy/k8s/00-namespace.yaml)
---
$(cat deploy/k8s/01-secret.yaml)
---
$(cat deploy/k8s/02-configmap.yaml)
---
$(cat deploy/k8s/03-postgres-pvc.yaml)
---
$(cat deploy/k8s/04-postgres.yaml)
---
$(cat deploy/k8s/05-control-plane.yaml)
---
$(cat deploy/k8s/06-worker.yaml)
---
$(cat deploy/k8s/07-ingress.yaml)
---
$(cat deploy/k8s/08-network-policy.yaml)
EOF
                            )"

                        # Trigger rolling restart with updated image
                        ssh -o StrictHostKeyChecking=no user@${K3S_MASTER} "\
                            sudo kubectl rollout restart -n personaos deployment/personaos-api && \
                            sudo kubectl rollout restart -n personaos deployment/personaos-worker"
                    """
                }
            }
        }

        stage('Trigger ArgoCD Sync') {
            steps {
                sh """
                    ssh -o StrictHostKeyChecking=no user@${K3S_MASTER} "\
                        sudo kubectl patch application -n argocd personaos --type merge \
                        -p '{\\"metadata\\":{\\"annotations\\":{\\"argocd.argoproj.io/refresh\\":\\"hard\\"}}}' 2>/dev/null || true"
                """
            }
        }

        stage('Verify Deployment Health') {
            steps {
                sh """
                    # Wait for API rollout
                    ssh -o StrictHostKeyChecking=no user@${K3S_MASTER} "\
                        sudo kubectl rollout status -n personaos deployment/personaos-api --timeout=120s"
                """
            }
        }
    }

    post {
        success {
            echo "✅ PersonaOS build and deployment #${BUILD_NUMBER} succeeded on K3s (PVE2 nodes)."
            echo "Active Image: ${IMAGE_FULL}:${IMAGE_TAG}"
        }
        failure {
            echo "❌ PersonaOS build and deployment #${BUILD_NUMBER} failed."
        }
        always {
            sh """
                docker rmi ${IMAGE_FULL}:${IMAGE_TAG} 2>/dev/null || true
            """
        }
    }
}
