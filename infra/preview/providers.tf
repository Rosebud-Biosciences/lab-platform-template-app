provider "aws" {
  region = var.region
}

provider "neon" {
  api_key = var.neon_api_key
}

# The shared cluster already exists (your prod platform stack), so the
# kubernetes/helm/kubectl providers read it via a data source.
data "aws_eks_cluster" "shared" {
  name = var.cluster_name
}

provider "kubernetes" {
  host                   = data.aws_eks_cluster.shared.endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.shared.certificate_authority[0].data)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", var.cluster_name, "--region", var.region]
  }
}

# helm provider 3: kubernetes/exec are object attributes (`=`).
provider "helm" {
  kubernetes = {
    host                   = data.aws_eks_cluster.shared.endpoint
    cluster_ca_certificate = base64decode(data.aws_eks_cluster.shared.certificate_authority[0].data)

    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", var.cluster_name, "--region", var.region]
    }
  }
}

provider "kubectl" {
  host                   = data.aws_eks_cluster.shared.endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.shared.certificate_authority[0].data)
  load_config_file       = false

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", var.cluster_name, "--region", var.region]
  }
}
